# src/autobots/ingestion/realtime_indexer.py
import asyncio
import uuid
from collections import defaultdict
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from bson import ObjectId
from cachetools import LRUCache
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from pymongo.errors import OperationFailure

from src.autobots.conn.chroma.chroma import Document
from src.autobots.conn.neo4j.ontology import ExtractedGraph
from src.autobots.conn.weaviate.weaviate_store import WeaviateStore
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.ingestion.event_result_hydrator import hydrate_event_result_from_s3
from src.autobots.ingestion.neo4j_loader import Neo4jLoader
from src.autobots.ingestion.neo4j_transformer import (
    transform_action_doc_to_graph,
    transform_action_graph_doc_to_graph,
    transform_event_result_doc_to_graph,
)
from src.autobots.ingestion.realtime_indexing_state import RealtimeIndexingStateCRUD
from src.autobots.user.user_orm_model import UserDoc, UserORM
from src.autobots.data_model.app_state import AppState

# [V13 PERF] Corrected projection for EventResults. $slice is not supported on nested paths.
# We fetch the parent sub-document and slice in the transformer.
PROJECTION = {
    "_id": 1,
    "user_id": 1,
    "file_url": 1,
    "updated_at": 1,
    "name": 1,
    "title": 1,
    "description": 1,
    "status": 1,
    "type": 1,
    "result.input": 1,
    "result.output": 1,
    "result.results": 1,
}


# --- New Data Model for Multi-Target Transformation ---
class TransformedData(BaseModel):
    weaviate_docs: List[Document] = Field(default_factory=list)
    neo4j_graph: Optional[ExtractedGraph] = None


# --- Helper Functions and Classes ---


class ClosableLRUCache(LRUCache):
    """An LRU Cache that safely calls .close() on evicted items and tracks tasks."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Track pending close tasks to ensure graceful shutdown.
        self._pending_close_tasks: Set[asyncio.Task] = set()

    async def _safe_close_and_track(self, store_to_close: WeaviateStore, key: str):
        try:
            await store_to_close.close()
            logger.bind(user_id=key).info("Evicted and closed stale Weaviate store from cache.")
        except Exception:
            logger.bind(user_id=key).exception("Async close op failed for evicted store.")
        finally:
            # Remove self from the tracking set once done.
            self._pending_close_tasks.discard(asyncio.current_task())

    def popitem(self):
        key, value = super().popitem()
        if hasattr(value, "close") and asyncio.iscoroutinefunction(value.close):
            # Explicitly get the running event loop before creating a task
            # to prevent a RuntimeError if popitem is called from a non-async context.
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._safe_close_and_track(value, key))
                self._pending_close_tasks.add(task)
            except RuntimeError:
                # If no loop is running, this is the most robust fallback.
                # It prevents a resource leak under all conditions, including shutdown.
                logger.bind(user_id=key).warning(
                    "No running event loop, closing store synchronously."
                )
                with suppress(Exception):
                    asyncio.run(self._safe_close_and_track(value, key))
        return key, value

    async def await_pending_closes(self):
        if self._pending_close_tasks:
            logger.info(f"Awaiting {len(self._pending_close_tasks)} pending store close tasks...")
            await asyncio.gather(*self._pending_close_tasks, return_exceptions=True)
            logger.info("All pending store close tasks completed.")


def _sanitize(text: Optional[str]) -> str:
    if not text:
        return ""
    # Coerce input to a string to prevent crashes on non-string types
    # like integers from malformed "poison pill" documents.
    if not isinstance(text, str):
        text = str(text)
    return " ".join(text.translate({10: " ", 13: " ", 0: None}).split())


# The collection_name is now passed to scope the document type and UUID.
def _create_weaviate_doc(
    page_content: str, doc: Dict[str, Any], collection_name: str, doc_type: str
) -> Optional[Document]:
    sanitized_content = _sanitize(page_content)
    if not sanitized_content:
        return None
    # Future enhancement: If a document has a soft-delete flag, handle it here.
    # if doc.get("is_deleted", False):
    #     # Return a special marker or handle deletion logic.
    #     return None
    metadata = {
        "document_id": str(doc.get("_id")),
        "source": _sanitize(doc.get("name") or doc.get("title"))
        or f"{doc_type}_{str(doc.get('_id'))}",
        "doc_type": doc_type,
        "mongo_collection": collection_name,  # Add source collection for context
        "user_id": str(doc.get("user_id")),
    }
    return Document(page_content=sanitized_content[:50000], metadata=metadata)


# --- Transformer Functions ---


def _transform_actions_doc(doc: Dict[str, Any]) -> TransformedData:
    page_content = f"Action: {doc.get('name', '')}\nDescription: {doc.get('description', '')}"
    weaviate_doc = _create_weaviate_doc(page_content, doc, "Actions", "action_definition")
    neo4j_graph = transform_action_doc_to_graph(doc)

    return TransformedData(
        weaviate_docs=[weaviate_doc] if weaviate_doc else [], neo4j_graph=neo4j_graph
    )


def _transform_action_graphs_doc(doc: Dict[str, Any]) -> TransformedData:
    page_content = f"Workflow: {doc.get('name', '')}\nDescription: {doc.get('description', '')}"
    weaviate_doc = _create_weaviate_doc(page_content, doc, "ActionGraphs", "workflow_definition")
    neo4j_graph = transform_action_graph_doc_to_graph(doc)

    return TransformedData(
        weaviate_docs=[weaviate_doc] if weaviate_doc else [], neo4j_graph=neo4j_graph
    )


async def _transform_event_results_doc(
    doc: Dict[str, Any], user: UserORM, db: AsyncIOMotorDatabase, ctx: Context, app_state: AppState
) -> TransformedData:
    # Hydrate the full document from S3 if needed
    # This single call makes the rest of the function compatible with both
    # old and new storage patterns by ensuring we always operate on the full data.
    full_doc = await hydrate_event_result_from_s3(doc, user, ctx)

    # ────────────────────────────────────────────────────────────────
    #  🔹  VERSION-STAMP  ‑  helps verify that the NEW code is loaded
    # ────────────────────────────────────────────────────────────────
    logger.bind(ctx=ctx).info(
        "--- Transformer v2025-10-12-multi-turn-fix engaged "
        f"(doc_id={full_doc.get('_id')}, type={full_doc.get('type')})"
    )

    if full_doc.get("status") != "success":
        return TransformedData()

    weaviate_docs = []
    run_type = full_doc.get("type")

    if run_type == "action_run":
        parent_id_str = str(full_doc.get("_id"))
        result_data = full_doc.get("result", {}) or {}
        multi_turn_results = result_data.get("results", [])

        if multi_turn_results and isinstance(multi_turn_results, list):
            logger.bind(ctx=ctx).info(
                f"Processing multi-turn EventResult {parent_id_str} with {len(multi_turn_results)} turns."
            )
            for i, turn in enumerate(multi_turn_results):
                input_text = _sanitize(turn.get("input", {}).get("text", ""))
                output_blocks = turn.get("output", {}).get("blocks", []) or []
                output_texts = [
                    _sanitize(block.get("text")) for block in output_blocks if block.get("text")
                ]

                if not output_texts:
                    continue

                page_content = f"Turn {i + 1} in action run '{full_doc.get('name', '')}':\nInput: {input_text}\nOutput: {' '.join(output_texts)}"

                # Create a unique sub-document ID for each turn's chunk to prevent UUID collisions.
                turn_doc_for_meta = {**full_doc, "_id": f"{parent_id_str}_turn_{i}"}

                weaviate_doc = _create_weaviate_doc(
                    page_content, turn_doc_for_meta, "EventResults", "run_result_turn"
                )
                if weaviate_doc:
                    weaviate_doc.metadata.update({"parent_run_id": parent_id_str, "turn_index": i})
                    weaviate_docs.append(weaviate_doc)
        else:
            # Fallback for single-turn action_runs
            input_text = _sanitize(str(result_data.get("input", {})))
            output_sub_doc = result_data.get("output", {}) or {}
            output_blocks = (output_sub_doc.get("blocks", []) or [])[:50]
            output_texts = [
                _sanitize(block.get("text")) for block in output_blocks if block.get("text")
            ]
            page_content = f"Execution Result for Action: {full_doc.get('name', '')}\nInput: {input_text}\nOutput: {' '.join(output_texts)}"
            weaviate_doc = _create_weaviate_doc(
                page_content, full_doc, "EventResults", "run_result"
            )
            if weaviate_doc:
                weaviate_docs.append(weaviate_doc)

    elif run_type == "action_graph_run":
        # FIX: Create a "summary" document for the entire graph run first.
        # This document will contain the top-level searchable fields like name and description.
        graph_run_result = full_doc.get("result", {}) or {}
        input_text = _sanitize(str(graph_run_result.get("input", {})))

        # The E2E test's 'description' field lives at the top level of the Mongo doc.
        # Let's include it in the summary.
        run_description = _sanitize(full_doc.get("description", ""))

        summary_page_content = f"Execution Summary for Workflow: {full_doc.get('name', '')}\nDescription: {run_description}\nInitial Input: {input_text}"

        # To prevent UUID collision with the root Neo4j node (which uses the same _id),
        # we create a new dict with a modified _id specifically for the summary chunk's metadata.
        parent_id_str = str(full_doc.get("_id"))
        summary_doc_for_meta = {**full_doc, "_id": f"{parent_id_str}_summary"}

        summary_weaviate_doc = _create_weaviate_doc(
            summary_page_content, summary_doc_for_meta, "EventResults", "workflow_run_summary"
        )
        if summary_weaviate_doc:
            weaviate_docs.append(summary_weaviate_doc)

        # ... (same multi-chunk logic as before) ...
        graph_output = full_doc.get("result", {}).get("output", {})
        if isinstance(graph_output, dict):
            for node_id, node_result in graph_output.items():
                if not isinstance(node_result, dict):
                    continue

                # --- STRATEGY B: Create one chunk PER TURN ---
                # 1. Collect all blocks from historical and final turns
                all_blocks = []
                historic_turns = node_result.get("results", []) or []
                for turn in historic_turns:
                    if isinstance(turn, dict):
                        all_blocks.extend(turn.get("output", {}).get("blocks", []) or [])

                all_blocks.extend(node_result.get("output", {}).get("blocks", []) or [])

                # 2. De-duplicate blocks by text content
                seen_texts = set()
                unique_blocks = []
                for block in all_blocks:
                    text_content = block.get("text")
                    if isinstance(block, dict) and text_content and text_content not in seen_texts:
                        unique_blocks.append(block)
                        seen_texts.add(text_content)

                if not unique_blocks:
                    continue

                # 3. Create a separate Weaviate document for each unique turn/block
                node_name = node_result.get("name", node_id)
                parent_id_str = str(full_doc.get("_id"))

                for i, block in enumerate(unique_blocks):
                    page_content = f"Result from Workflow '{full_doc.get('name', '')}', Step '{node_name}', Turn {i + 1}:\n{_sanitize(block.get('text'))}"

                    # Create a unique sub-document ID for each turn to generate a unique UUID
                    turn_doc_for_meta = {**full_doc, "_id": f"{parent_id_str}_{node_id}_turn_{i}"}

                    weaviate_doc = _create_weaviate_doc(
                        page_content, turn_doc_for_meta, "EventResults", "workflow_step_result_turn"
                    )
                    if weaviate_doc:
                        weaviate_doc.metadata.update(
                            {
                                "parent_run_id": parent_id_str,
                                "graph_node_id": node_id,
                                "turn_index": i,
                            }
                        )
                        weaviate_docs.append(weaviate_doc)

    # Generate the graph representation for Neo4j.
    # This will be None for non-successful runs or non-graph types.
    neo4j_graph = await transform_event_result_doc_to_graph(full_doc, user, db, ctx, app_state)

    return TransformedData(weaviate_docs=weaviate_docs, neo4j_graph=neo4j_graph)


def _transform_chats_doc(doc: Dict[str, Any]) -> TransformedData:
    messages = [
        f"{_sanitize(msg.get('role', 'user')).capitalize()}: {_sanitize(msg.get('content'))}"
        for msg in doc.get("messages", [])[-100:]
        if msg.get("content")
    ]
    page_content = f"Chat Title: {_sanitize(doc.get('title'))}\n" + "\n".join(messages)
    weaviate_doc = _create_weaviate_doc(page_content, doc, "Chats", "chat_transcript")
    return TransformedData(weaviate_docs=[weaviate_doc] if weaviate_doc else [])


# --- Main Indexer Class ---


class RealtimeIndexer:
    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.settings = SettingsProvider.sget()
        self.state_crud = RealtimeIndexingStateCRUD(db)
        self.weaviate_store_cache = ClosableLRUCache(
            maxsize=self.settings.INGESTION_WORKER_MONGO_INDEXER_WEAVIATE_CACHE_SIZE
        )
        self._cache_lock = asyncio.Lock()
        self._retry_queue: Dict[str, Dict[str, Dict]] = defaultdict(dict)
        self._neo4j_loader: Optional[Neo4jLoader] = None
        # RACE CONDITION: Add a lock to protect the shared retry queue.
        self._retry_queue_lock = asyncio.Lock()
        self.transformer_map = {
            "Actions": _transform_actions_doc,
            "ActionGraphs": _transform_action_graphs_doc,
            "EventResults": _transform_event_results_doc,
            "Chats": _transform_chats_doc,
        }

    async def _get_neo4j_loader(self) -> Neo4jLoader:
        if not self._neo4j_loader:
            self._neo4j_loader = Neo4jLoader()
        return self._neo4j_loader

    def _get_bootstrap_cursor(self) -> Tuple[datetime, ObjectId]:
        mode = self.settings.INGESTION_WORKER_MONGO_INDEXER_BOOTSTRAP_MODE
        min_id = ObjectId("000000000000000000000000")
        # Ensure all bootstrap datetimes are naive UTC to prevent BSON error.
        if mode == "full_history":
            return datetime.utcfromtimestamp(0), min_id
        elif mode == "hours_ago":
            ts = datetime.utcnow() - timedelta(
                hours=self.settings.INGESTION_WORKER_MONGO_INDEXER_BOOTSTRAP_HOURS_BACK
            )
            return ts, min_id
        else:  # "current"
            overlap_seconds = max(
                self.settings.INGESTION_WORKER_MONGO_INDEXER_POLL_INTERVAL_SECONDS * 2, 60
            )
            ts = datetime.utcnow() - timedelta(seconds=overlap_seconds)
            return ts, min_id

    async def _index_user_documents_with_retry(
        self, user_id: str, docs_to_index: List[Document], ctx: Context
    ) -> bool:
        for attempt in range(3):
            try:
                store = await self._get_weaviate_store(user_id)

                # WORKAROUND: To avoid the non-idempotent `insert()` path in weaviate_client,
                # we manually handle idempotency here for single-document batches.
                if len(docs_to_index) == 1:
                    doc_to_check = docs_to_index[0]
                    doc_id = doc_to_check.metadata.get("document_id")
                    if doc_id:  # Use the centralized helper for consistency
                        deterministic_uuid = self._generate_weaviate_uuid(
                            doc_to_check.metadata, user_id
                        )
                        existing = await store.get(ids=[deterministic_uuid])
                        if existing and existing[0] is not None:
                            await store.update_documents(
                                ids=[deterministic_uuid], documents=docs_to_index
                            )
                            return True  # Manually handled, return success.

                ids_to_add = [
                    self._generate_weaviate_uuid(d.metadata, user_id) for d in docs_to_index
                ]
                successful_ids = await store.add_documents(docs_to_index, ids=ids_to_add)

                if len(successful_ids) != len(docs_to_index):
                    logger.bind(ctx=ctx, user_id=user_id).error(
                        f"Partial write for user. Expected {len(docs_to_index)}, got {len(successful_ids)}."
                    )
                    return False
                return True
            except Exception as e:
                if attempt == 2:
                    logger.bind(ctx=ctx, user_id=user_id).exception(
                        "Final attempt to index docs failed."
                    )
                    return False
                wait_time = 2**attempt
                logger.bind(ctx=ctx, user_id=user_id).warning(
                    f"Retry {attempt + 1}/3 failed for user: {e}. Retrying in {wait_time}s."
                )
                await asyncio.sleep(wait_time)
        return False

    async def _get_weaviate_store(self, user_id: str) -> WeaviateStore:
        # Check cache without lock first for performance
        store = self.weaviate_store_cache.get(user_id)
        if store:
            return store

        # If not in cache, create it outside the lock to avoid blocking
        new_store = await WeaviateStore.create_store(
            collection_name=self.settings.INGESTION_WORKER_MONGO_INDEXER_WEAVIATE_COLLECTION_NAME,
            user_id=user_id,
        )

        # Acquire lock only to safely update the cache
        async with self._cache_lock:
            # The setdefault call is atomic and race-safe
            store_in_cache = self.weaviate_store_cache.setdefault(user_id, new_store)
            # If another task won the race, close the one we just created to prevent leaks
            if store_in_cache is not new_store:
                asyncio.create_task(new_store.close())
            return store_in_cache

    async def _index_user_graphs_with_retry(
        self,
        user_id: str,
        graphs_and_metadata: List[Tuple[ExtractedGraph, str, List[str]]],
        ctx: Context,
    ) -> bool:
        # Unlike Weaviate, Neo4j transactions are more robust, so a simpler loop is fine.
        for attempt in range(3):
            try:
                loader = await self._get_neo4j_loader()
                tasks = []
                for graph, document_id, chunk_uuids in graphs_and_metadata:  # Unpack real UUIDs
                    if not graph or not graph.nodes:
                        continue

                    # If for some reason the UUID list is empty, use a fallback to prevent failure.
                    final_chunk_uuids = chunk_uuids or [document_id]

                    tasks.append(
                        loader.load(
                            processed_data=graph,
                            user_id=user_id,
                            collection_name="RealtimeGraphData",
                            document_id=document_id,
                            chunk_uuids=final_chunk_uuids,  # Use the real UUIDs
                        )
                    )
                if tasks:
                    # FIX: Check the results of the gather to ensure all load operations succeeded.
                    results = await asyncio.gather(*tasks)
                    if not all(results):
                        raise RuntimeError("One or more graphs failed to load into Neo4j.")
                return True
            except Exception as e:
                if attempt == 2:
                    logger.bind(ctx=ctx, user_id=user_id).exception(
                        "Final attempt to index graphs to Neo4j failed."
                    )
                    return False
                wait_time = 2**attempt
                logger.bind(ctx=ctx, user_id=user_id).warning(
                    f"Neo4j write failed: {e}. Retrying in {wait_time}s."
                )
                await asyncio.sleep(wait_time)
        return False

    def _generate_weaviate_uuid(self, metadata: Dict[str, Any], user_id: str) -> str:
        """
        CENTRALIZED HELPER: Generates the deterministic UUID for a document/chunk.
        This is the single source of truth for UUID generation in the indexer.
        """
        mongo_collection = metadata["mongo_collection"]
        document_id = metadata["document_id"]
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{user_id}:{mongo_collection}:{document_id}"))

    async def _process_retry_queue(self, ctx: Context):
        # RACE CONDITION: Perform all reads and modifications under the lock.
        async with self._retry_queue_lock:
            if not self._retry_queue:
                return

            logger.bind(ctx=ctx).info(
                f"Processing retry queue with {sum(len(docs) for docs in self._retry_queue.values())} items."
            )

            # Create a snapshot of documents to process outside the lock
            docs_to_retry_by_user = defaultdict(list)
            for collection_name, docs in self._retry_queue.items():
                for doc_id, item in docs.items():
                    docs_to_retry_by_user[item["doc"].metadata["user_id"]].append(item["doc"])

        if not docs_to_retry_by_user:
            return

        user_tasks = [
            self._index_user_documents_with_retry(user_id, docs, ctx)
            for user_id, docs in docs_to_retry_by_user.items()
        ]
        results = await asyncio.gather(*user_tasks)
        successful_users = {
            user_id for user_id, success in zip(docs_to_retry_by_user.keys(), results) if success
        }

        # RACE CONDITION: Lock again for modification.
        async with self._retry_queue_lock:
            for collection_name, docs in list(self._retry_queue.items()):
                for doc_id, item in list(docs.items()):
                    user_id = item["doc"].metadata["user_id"]
                    if user_id in successful_users:
                        del self._retry_queue[collection_name][doc_id]
                    else:
                        item["retry_count"] += 1
                        if item["retry_count"] > 3:
                            logger.bind(ctx=ctx).error(
                                f"Document {doc_id} from {collection_name} failed max retries. Dropping from retry queue."
                            )
                            del self._retry_queue[collection_name][doc_id]
                if not self._retry_queue[collection_name]:
                    del self._retry_queue[collection_name]

    async def _process_collection_updates(self, ctx: Context, collection_name: str):
        # Restore the time-based cursor to handle document UPDATES correctly.
        state = await self.state_crud.get_state(collection_name)
        if state:
            current_ts = state.last_processed_cursor_ts
            if current_ts.tzinfo:
                current_ts = current_ts.replace(tzinfo=None)
            try:
                current_id = ObjectId(state.last_processed_cursor_id)
            except Exception:
                current_id = state.last_processed_cursor_id
        else:
            current_ts, current_id = self._get_bootstrap_cursor()

        # This guard is always active to prevent processing historical data.
        bootstrap_ts, _ = self._get_bootstrap_cursor()

        while True:
            collection = self.db[collection_name]

            # This robust query handles new documents, updated documents, and respects the bootstrap time.
            query_parts = [
                {"updated_at": {"$gte": bootstrap_ts}},
                {
                    "$or": [
                        {"indexed": {"$ne": True}},
                        {"updated_at": {"$gt": current_ts}},
                        {"updated_at": current_ts, "_id": {"$gt": current_id}},
                    ]
                },
            ]

            if collection_name in {"EventResults"}:
                query_parts.append({"status": "success"})

            query = {"$and": query_parts}

            # --- Inject tenant filter without destroying existing predicates ---
            target_ids = self.settings.INGESTION_WORKER_MONGO_INDEXER_TARGET_USER_IDS
            if target_ids:
                # Append the user whitelist condition to the existing $and list
                query["$and"].append({"user_id": {"$in": target_ids}})
            else:
                # Extend the $and list with guards to ensure a document has a valid user_id
                query["$and"].extend(
                    [
                        {"user_id": {"$exists": True}},
                        {"user_id": {"$ne": None}},
                    ]
                )

            final_projection = PROJECTION.copy()
            if collection_name == "Chats":
                # Dynamically add the messages field with a $slice operator for Chats.
                final_projection["messages"] = {"$slice": -100}

            # Ensure the transformer receives the fields it needs for graph creation.
            if collection_name == "ActionGraphs":
                final_projection.update({"node_details": 1, "graph": 1})

            # Removed the invalid re-introduction of a dotted-path $slice.
            # The slicing is correctly handled in the Python transformer.
            # if collection_name == "EventResults":
            #     final_projection["result.output.blocks"] = {"$slice": 50}

            # RESILIENCE: Wrap the hint in a try/except to prevent crashes on first boot
            # if the index is still building in the background.
            try:
                cursor = (
                    collection.find(query, final_projection)
                    # The hint must also be conditional to exactly match the index that exists for the collection.
                    .hint(
                        # Correctly match the full index for EventResults.
                        [("status", 1), ("indexed", 1), ("updated_at", 1), ("_id", 1)]
                        if collection_name in {"EventResults"}
                        # Correctly match the index for other collections.
                        else [("indexed", 1), ("updated_at", 1), ("_id", 1)]
                    )
                    .sort([("updated_at", 1), ("_id", 1)])
                    .limit(self.settings.INGESTION_WORKER_MONGO_INDEXER_BATCH_SIZE)
                )
                docs_batch = await cursor.to_list(
                    length=self.settings.INGESTION_WORKER_MONGO_INDEXER_BATCH_SIZE
                )
            except OperationFailure as e:
                # This typically happens if the index is not ready yet.
                logger.bind(ctx=ctx, collection=collection_name).warning(
                    f"Could not use index hint, retrying without it. Error: {e}"
                )
                cursor = (
                    collection.find(query, final_projection)
                    .sort([("updated_at", 1), ("_id", 1)])
                    .limit(self.settings.INGESTION_WORKER_MONGO_INDEXER_BATCH_SIZE)
                )
                docs_batch = await cursor.to_list(
                    length=self.settings.INGESTION_WORKER_MONGO_INDEXER_BATCH_SIZE
                )

            if not docs_batch:
                break

            next_batch_start_ts = docs_batch[-1]["updated_at"]
            if next_batch_start_ts.tzinfo is not None:
                next_batch_start_ts = next_batch_start_ts.replace(tzinfo=None)
            next_batch_start_id = docs_batch[-1]["_id"]

            weaviate_docs_by_user: Dict[str, List[Document]] = {}
            # FIX: Store tuples of (graph, original_doc_id) to ensure stable IDs for Neo4j.
            neo4j_graphs_by_user: Dict[
                str, List[Tuple[ExtractedGraph, str, List[str]]]  # (graph, doc_id, chunk_uuids)
            ] = defaultdict(list)

            transformer = self.transformer_map.get(collection_name)
            # --- FIX: Unconditionally fetch User objects for the entire batch ---
            # The transformation loop requires a user object to proceed, even if the
            # transformer function itself doesn't use it.
            user_map: Dict[str, UserORM] = {}
            user_ids_in_batch = {str(d["user_id"]) for d in docs_batch if "user_id" in d}
            if user_ids_in_batch:
                user_docs = (
                    await self.db["Users"]
                    .find({"_id": {"$in": list(user_ids_in_batch)}})
                    .to_list(length=None)
                )
                for user_doc in user_docs:
                    # FIX: Validate against UserDoc, which is the correct model
                    # for documents from the 'Users' MongoDB collection and
                    # is correctly configured to handle the '_id' field.
                    validated_user_doc = UserDoc.model_validate(user_doc)
                    user_map[str(validated_user_doc.id)] = validated_user_doc

            # --- SINGLE KNOB CHECK ---
            targets_for_collection = self.settings.INGESTION_WORKER_MONGO_INDEXER_TARGETS.get(
                collection_name, []
            )
            if transformer:
                for doc in docs_batch:
                    # RESILIENCE: Handle "poison pill" documents. If a single document
                    # fails to transform, log the error and skip it, preventing the entire
                    # collection from being stalled indefinitely.
                    try:
                        user_id = str(doc.get("user_id"))
                        user = user_map.get(user_id)
                        if not user:
                            logger.bind(
                                ctx=ctx, collection=collection_name, doc_id=str(doc.get("_id"))
                            ).warning(
                                f"Skipping document: User {user_id} not found in Users collection."
                            )
                            continue  # Skip doc if user not found

                        if asyncio.iscoroutinefunction(transformer):
                            document_state = AppState()
                            document_id = str(doc.get("_id")) if doc.get("_id") is not None else None
                            run_type = doc.get("type")
                            if run_type == "action_run":
                                document_state.action_run_id = document_id
                                result = doc.get("result") or {}
                                document_state.action_id = (
                                    str(result.get("_id") or result.get("id"))
                                    if result.get("_id") or result.get("id")
                                    else None
                                )
                            elif run_type == "action_graph_run":
                                document_state.action_graph_result_id = document_id
                                result = doc.get("result") or {}
                                document_state.action_graph_id = (
                                    str(result.get("_id") or result.get("id"))
                                    if result.get("_id") or result.get("id")
                                    else None
                                )
                            
                            transformed_data = await transformer(
                                doc,
                                user,
                                self.db,
                                ctx,
                                document_state,
                            )
                        else:
                            transformed_data = transformer(doc)

                        if transformed_data and (
                            transformed_data.weaviate_docs or transformed_data.neo4j_graph
                        ):
                            # Guard Weaviate writes with the target check.
                            if (
                                "weaviate" in targets_for_collection
                                and transformed_data.weaviate_docs
                            ):
                                weaviate_docs_by_user.setdefault(user_id, []).extend(
                                    transformed_data.weaviate_docs
                                )
                            if "neo4j" in targets_for_collection and transformed_data.neo4j_graph:
                                # CORRECTED: Generate UUIDs from the final Weaviate docs list.
                                # This is the single source of truth that ensures alignment.
                                chunk_uuids_for_neo4j = [
                                    self._generate_weaviate_uuid(d.metadata, user_id)
                                    for d in transformed_data.weaviate_docs
                                ]
                                neo4j_graphs_by_user[user_id].append(
                                    (
                                        transformed_data.neo4j_graph,
                                        str(doc["_id"]),
                                        chunk_uuids_for_neo4j,
                                    )
                                )

                    except Exception:
                        doc_id = doc.get("_id", "unknown")
                        logger.bind(
                            ctx=ctx, collection=collection_name, doc_id=str(doc_id)
                        ).exception(
                            "Failed to transform document (poison pill). It will be skipped."
                        )
                        continue

            # RESILIENCE: Handle the infinite loop case where a batch contains
            # zero processable documents after transformation (e.g., all are poison pills).
            if not weaviate_docs_by_user and not neo4j_graphs_by_user:
                logger.bind(ctx=ctx, collection=collection_name).info(
                    "Batch yielded no indexable documents. Advancing cursor to prevent re-processing."
                )
                # Unconditionally advance the cursor and continue to the next batch.
                state = await self.state_crud.update_cursor(
                    collection_name, next_batch_start_ts, str(next_batch_start_id)
                )
                current_ts = state.last_processed_cursor_ts
                current_id = ObjectId(state.last_processed_cursor_id)
                if len(docs_batch) < self.settings.INGESTION_WORKER_MONGO_INDEXER_BATCH_SIZE:
                    break
                else:
                    continue

            # --- Execute Loading Tasks Concurrently ---
            weaviate_user_tasks = [
                self._index_user_documents_with_retry(user_id, docs, ctx)
                for user_id, docs in weaviate_docs_by_user.items()
            ]
            weaviate_results = await asyncio.gather(*weaviate_user_tasks)

            neo4j_user_results: list[bool] = []
            if "neo4j" in targets_for_collection and neo4j_graphs_by_user:
                logger.bind(ctx=ctx).info(
                    f"Loading {sum(len(g) for g in neo4j_graphs_by_user.values())} graphs into Neo4j."
                )
                neo4j_user_tasks = [
                    self._index_user_graphs_with_retry(user_id, graphs_and_metadata, ctx)
                    for user_id, graphs_and_metadata in neo4j_graphs_by_user.items()
                ]
                neo4j_user_results = await asyncio.gather(*neo4j_user_tasks)

            # --- Determine overall success for each user, considering all targets ---
            weaviate_success_by_user = {
                user_id: success
                for (user_id, _), success in zip(weaviate_docs_by_user.items(), weaviate_results)
            }
            neo4j_success_by_user = {
                user_id: success
                for (user_id, _), success in zip(neo4j_graphs_by_user.items(), neo4j_user_results)
            }

            all_user_ids = set(weaviate_docs_by_user.keys()) | set(neo4j_graphs_by_user.keys())
            successful_user_ids = set()

            for user_id in all_user_ids:
                is_weaviate_ok = weaviate_success_by_user.get(user_id, True)  # True if not a target
                is_neo4j_ok = neo4j_success_by_user.get(user_id, True)  # True if not a target
                if is_weaviate_ok and is_neo4j_ok:
                    successful_user_ids.add(user_id)

            async with self._retry_queue_lock:
                # This retry queue logic can be simplified or removed, as the main
                # loop will now naturally retry failed (un-indexed) documents.
                pass

            # --- FIX: Mark only fully successful documents as indexed ---
            successful_mongo_ids = [
                doc["_id"] for doc in docs_batch if str(doc.get("user_id")) in successful_user_ids
            ]

            if successful_mongo_ids:
                await collection.update_many(
                    {"_id": {"$in": successful_mongo_ids}}, {"$set": {"indexed": True}}
                )
                logger.bind(ctx=ctx, collection=collection_name).success(
                    f"Successfully processed and marked {len(successful_mongo_ids)} documents as indexed."
                )

            # --- FIX: ALWAYS advance the cursor after processing a batch ---
            # This ensures that even if a batch is skipped or fails, we don't re-process it indefinitely.
            state = await self.state_crud.update_cursor(
                collection_name, next_batch_start_ts, str(next_batch_start_id)
            )
            # Update in-memory variables for the next iteration of the loop.
            current_ts = state.last_processed_cursor_ts
            current_id = ObjectId(state.last_processed_cursor_id)

            if len(docs_batch) < self.settings.INGESTION_WORKER_MONGO_INDEXER_BATCH_SIZE:
                break

    async def run_loop(self):
        logger.info("Real-time indexer loop is starting.")

        # FIX: Implement the master switch logic based on the INGESTION_WORKER_MONGO_INDEXER_TARGETS setting.
        # If no collections have any targets enabled, the indexer will not run.
        collections_to_watch = [
            collection
            for collection, targets in self.settings.INGESTION_WORKER_MONGO_INDEXER_TARGETS.items()
            if targets
        ]

        if not collections_to_watch:
            logger.warning(
                "Real-time indexer has no active collections in INGESTION_WORKER_MONGO_INDEXER_TARGETS. Loop will not run."
            )
            return

        logger.info(f"Indexer will watch the following collections: {collections_to_watch}")

        # TODO: After initial testing, add a health check endpoint to expose the status
        # of the last run for each collection. This would be valuable for monitoring.

        while True:
            ctx = Context(trace_id=f"indexer-{uuid.uuid4().hex[:8]}")
            logger.bind(ctx=ctx).debug("Starting new indexing cycle.")
            try:
                # Prioritize the retry queue before fetching new documents.
                await self._process_retry_queue(ctx)

                await asyncio.gather(
                    *[self._process_collection_updates(ctx, name) for name in collections_to_watch]
                )
            except asyncio.CancelledError:
                logger.info("Indexer loop cancelled.")
                break
            except Exception:
                logger.bind(ctx=ctx).exception("Unhandled error in indexing cycle.")

            await asyncio.sleep(self.settings.INGESTION_WORKER_MONGO_INDEXER_POLL_INTERVAL_SECONDS)

    async def close_all_stores(self):
        # RESILIENCE: Before closing active stores, await any pending close tasks from evicted items.
        await self.weaviate_store_cache.await_pending_closes()

        logger.info(f"Closing {len(self.weaviate_store_cache)} active Weaviate stores...")
        close_tasks = [store.close() for store in list(self.weaviate_store_cache.values())]
        await asyncio.gather(*close_tasks, return_exceptions=True)
        logger.info("Finished closing all Weaviate stores.")
