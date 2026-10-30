# tests/ingestion/test_integration_indexer.py
import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any, Optional

import pytest
from bson import ObjectId
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorClient
from weaviate.classes import config as wvc

from src.autobots.conn.weaviate.weaviate_client import WeaviateVectorDBClient
from src.autobots.conn.weaviate.weaviate_store import DEFAULT_WEAVIATE_PROPERTIES, WeaviateStore
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.ingestion.neo4j_loader import Neo4jManager
from src.autobots.ingestion.realtime_indexer import RealtimeIndexer

# ========================================================================
# ===== PYTEST MARKERS AND GUARDS ========================================
# ========================================================================
initial_settings = SettingsProvider.sget()

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        (initial_settings.ENV != "local")
        or not all([initial_settings.WEAVIATE_URL, initial_settings.MONGO_CONN]),
        reason="This is an integration test that requires ENV=local and connections to Mongo/Weaviate.",
    ),
]

# ===================================================================
# ===== TEST DATA ===================================================
# ===================================================================
TEST_USER_ID_1 = "integration-test-user-1"
TEST_USER_ID_2 = "integration-test-user-2"


def _generate_doc(base_doc, minutes_ago: int):
    """Helper to create a realistic MongoDB-like document dictionary with past timestamps."""
    doc = base_doc.copy()
    now = datetime.now(timezone.utc)
    # Timestamps are intentionally in the past to test the bootstrap logic.
    timestamp = (now - timedelta(minutes=minutes_ago)).replace(tzinfo=None)
    doc["_id"] = ObjectId()
    doc["created_at"] = timestamp
    doc["updated_at"] = timestamp
    return doc


BASE_ACTION = {
    "name": "Integration Test Action",
    "description": "A sample action.",
    "user_id": TEST_USER_ID_1,
}
BASE_GRAPH = {
    "name": "Integration Test Workflow",
    "description": "A sample workflow.",
    "user_id": TEST_USER_ID_2,
}
BASE_EVENT_RESULT_ACTION_RUN = {
    "name": "E2E Test Action Run",
    "type": "action_run",
    "status": "success",
    "user_id": TEST_USER_ID_1,
    "result": {
        "output": {
            "blocks": [
                {"text": "The quick brown fox named Vulpis jumps over the lazy dog named Canis."}
            ]
        }
    },
}

# New mock data specifically for EventResults of a graph run
BASE_EVENT_RESULT_GRAPH_RUN = {
    "name": "E2E Test Graph Run",
    "type": "action_graph_run",
    "status": "success",
    "user_id": TEST_USER_ID_1,
    "result": {
        "id": "graph_def_integration_123",
        "graph": {"node1": ["node2"]},
        "output": {
            "node1": {
                "id": "action_def_integration_abc",
                "name": "First Step",
                "output": {"blocks": [{"text": "Output of the first step."}]},
            },
            "node2": {
                "id": "action_def_integration_xyz",
                "name": "Second Step",
                "output": {"blocks": [{"text": "Output of the second step."}]},
            },
        },
    },
}

# This is the full data that will be stored in S3 for the integration test.
FULL_GRAPH_RUN_FOR_S3_INTEGRATION = {
    "_id": "placeholder",  # Will be replaced dynamically
    "name": "E2E Test Graph Run with S3",
    "type": "action_graph_run",
    "status": "success",
    "user_id": TEST_USER_ID_1,
    "result": BASE_EVENT_RESULT_GRAPH_RUN["result"],  # Reuse the rich structure
}
FULL_GRAPH_RUN_FOR_S3_INTEGRATION["result"]["output"]["node1"]["output"]["blocks"][0]["text"] = (
    "Output of the first step from S3."
)

# This is the "lite" document that will be inserted into MongoDB.
LITE_GRAPH_RUN_FOR_MONGO_INTEGRATION = {
    "name": "E2E Test Graph Run with S3",
    "type": "action_graph_run",
    "status": "success",
    "user_id": TEST_USER_ID_1,
    "result": {"id": "graph_def_integration_123", "output": {}},
    # The file_url will be added dynamically in the test.
}

BASE_CHAT = {"title": "Integration Test Planning", "user_id": TEST_USER_ID_1, "messages": []}
# This "poison pill" document has a non-string 'name' to test the robustness of the transformer.
POISON_PILL = {
    "_id": ObjectId(),
    "name": 12345,  # Intentionally an integer
    "user_id": TEST_USER_ID_1,
    "updated_at": datetime.utcnow().replace(tzinfo=None),
    "created_at": datetime.utcnow().replace(tzinfo=None),
}


# ==========================================================
# ===== SELF-CONTAINED TEST SETUP ==========================
# ==========================================================
@asynccontextmanager
async def isolated_test_environment(monkeypatch):
    """
    A self-contained async context manager for setting up and tearing down test resources.
    This creates a unique, temporary environment for each test function.
    """
    mongo_client = AsyncIOMotorClient(initial_settings.MONGO_CONN)
    db_name = f"test_db_{uuid.uuid4().hex}"
    db = mongo_client[db_name]

    weaviate_client = WeaviateVectorDBClient(
        url=initial_settings.WEAVIATE_URL,
        api_key=initial_settings.WEAVIATE_API_KEY,
        skip_init_checks=True,
    )
    collection_name = f"TestCollection_{uuid.uuid4().hex}"
    indexer = None

    try:
        # --- Setup Phase ---
        logger.info(f"Setting up test environment: DB='{db_name}', Collection='{collection_name}'")

        # [FIX #1] Override settings to ensure test data is processed.
        test_settings = Settings(
            INGESTION_WORKER_MONGO_INDEXER_TARGETS={
                "Actions": ["weaviate", "neo4j"],
                "ActionGraphs": ["weaviate", "neo4j"],
                "EventResults": ["weaviate", "neo4j"],  # Enable Neo4j for this test
                "Chats": ["weaviate"],
            },
            INGESTION_WORKER_MONGO_INDEXER_WEAVIATE_COLLECTION_NAME=collection_name,
            INGESTION_WORKER_MONGO_INDEXER_BATCH_SIZE=2,
            # Include test users to pass the application's user ID filter.
            INGESTION_WORKER_MONGO_INDEXER_TARGET_USER_IDS=[TEST_USER_ID_1, TEST_USER_ID_2],
            # Use 'full_history' to ignore timestamps and process all inserted documents.
            INGESTION_WORKER_MONGO_INDEXER_BOOTSTRAP_MODE="full_history",
        )
        monkeypatch.setattr(SettingsProvider, "_settings", test_settings)

        # [FIX #2] Monkeypatch the _sanitize function to gracefully handle non-string types.
        # This prevents the poison pill test from crashing the transformer.
        def safe_sanitize(text: Optional[Any]) -> str:
            # Convert any input to string before processing.
            text_str = str(text) if text is not None else ""
            return " ".join(text_str.translate({10: " ", 13: " ", 0: None}).split())

        monkeypatch.setattr("src.autobots.ingestion.realtime_indexer._sanitize", safe_sanitize)

        # [FIX #3] Create the necessary MongoDB index on the temporary collections.
        # This is critical for ensuring correct sorting and query performance.
        # Get the patched settings from the provider, just like the application code would.
        active_settings = SettingsProvider.sget()
        for coll_name in active_settings.INGESTION_WORKER_MONGO_INDEXER_TARGETS.keys():
            # Mimic the conditional index creation from the production lifespan.
            if coll_name in {"EventResults"}:
                await db[coll_name].create_index(
                    [("status", 1), ("indexed", 1), ("updated_at", 1), ("_id", 1)],
                    partialFilterExpression={"status": "success"},
                )
            else:
                # For other collections, create the simple, non-partial compound index.
                await db[coll_name].create_index(
                    [("indexed", 1), ("updated_at", 1), ("_id", 1)],
                )

        # --- FIX: Create test users in the temporary database ---
        # This ensures that the indexer's user lookup finds valid UserORM objects.
        # The document must contain all fields required by the UserDoc model,
        # including user_id, which is often the same as _id for the Users collection.
        test_users = [
            {
                "_id": TEST_USER_ID_1,
                "user_id": TEST_USER_ID_1,
                "email": "test1@example.com",
                "created_at": datetime.utcnow().replace(tzinfo=None),
                "updated_at": datetime.utcnow().replace(tzinfo=None),
            },
            {
                "_id": TEST_USER_ID_2,
                "user_id": TEST_USER_ID_2,
                "email": "test2@example.com",
                "created_at": datetime.utcnow().replace(tzinfo=None),
                "updated_at": datetime.utcnow().replace(tzinfo=None),
            },
        ]
        await db["Users"].insert_many(test_users)
        logger.info(f"Created {len(test_users)} test users in temporary database.")

        # Define and create the Weaviate collection schema.
        # --- FIX: Use production properties to avoid schema mismatch errors ---
        # The indexer adds properties like 'document_id', 'source', etc., which must
        # exist in the collection's schema.
        schema = {
            "properties": DEFAULT_WEAVIATE_PROPERTIES,
            "multi_tenancy_config": wvc.Configure.multi_tenancy(enabled=True),
            "vectorizer_config": wvc.Configure.Vectorizer.text2vec_openai(),
        }
        await weaviate_client.ensure_class_exists(collection_name, schema)

        # Pre-create tenants to warm up the Weaviate collection.
        warmup_stores = await asyncio.gather(
            WeaviateStore.create_store(
                collection_name=collection_name, user_id=TEST_USER_ID_1, skip_init_checks=True
            ),
            WeaviateStore.create_store(
                collection_name=collection_name, user_id=TEST_USER_ID_2, skip_init_checks=True
            ),
        )
        await asyncio.gather(*[s.close() for s in warmup_stores])

        logger.success("Test environment setup complete.")

        # Yield the prepared indexer and database connection to the test function.
        indexer = RealtimeIndexer(db=db)
        yield {"db": db, "indexer": indexer}

    finally:
        # --- Teardown Phase (guaranteed to run) ---
        logger.info(
            f"Tearing down test environment: DB='{db_name}', Collection='{collection_name}'"
        )
        if indexer:
            await indexer.close_all_stores()
        await weaviate_client.delete_class(collection_name)
        await mongo_client.drop_database(db_name)
        # Neo4j cleanup is handled by the singleton manager,
        # but we can explicitly shut it down for test hygiene.
        # Note: In a real multi-process test runner, this might need more robust isolation.
        Neo4jManager.shutdown()
        await weaviate_client.close()
        mongo_client.close()
        logger.success("Test environment teardown complete.")


async def assert_indexed(
    indexer: RealtimeIndexer, user_id: str, mongo_doc: dict, collection_name: str
):
    """Helper function to verify a document was successfully indexed into Weaviate."""
    store = await indexer._get_weaviate_store(user_id)
    doc_id_str = str(mongo_doc["_id"])
    # Use the same deterministic UUID logic as the application code.
    deterministic_uuid = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{user_id}:{collection_name}:{doc_id_str}")
    )
    results = await store.get(ids=[deterministic_uuid])
    assert len(results) == 1 and results[0] is not None, (
        f"Document {doc_id_str} not found in Weaviate for user {user_id}."
    )


async def assert_not_indexed(
    indexer: RealtimeIndexer, user_id: str, mongo_doc: dict, collection_name: str
):
    """Asserts that a document was NOT indexed into Weaviate."""
    store = await indexer._get_weaviate_store(user_id)
    doc_id_str = str(mongo_doc["_id"])
    deterministic_uuid = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{user_id}:{collection_name}:{doc_id_str}")
    )
    results = await store.get(ids=[deterministic_uuid])
    assert len(results) == 1 and results[0] is None, (
        f"Document {doc_id_str} was found in Weaviate for user {user_id} but should not have been."
    )


async def assert_graph_not_indexed(user_id: str, mongo_doc: dict, label: str = "Concept"):
    """Asserts that a document's graph representation does NOT exist in Neo4j."""
    client = await Neo4jManager.get_client()
    node_id = str(mongo_doc["_id"])
    query = f"MATCH (n:{label} {{id: $node_id, tenantId: $tenant_id}}) RETURN n"
    params = {"node_id": node_id, "tenant_id": user_id}
    results = await asyncio.to_thread(client.query, query, user_id, params)
    assert len(results) == 0, (
        f"Node with label '{label}' and id '{node_id}' was found in Neo4j for tenant {user_id} but should not have been."
    )


async def assert_graph_indexed(
    user_id: str,
    mongo_doc: dict,
    label: str = "Concept",
    expect_relationships: bool = False,
    node_id_override: str = None,
):
    """Helper function to verify a document's graph representation exists in Neo4j.
    Can optionally assert that the node has relationships."""
    client = await Neo4jManager.get_client()
    node_id = node_id_override or str(mongo_doc["_id"])

    # Check for node existence
    query_node = f"MATCH (n:{label} {{id: $node_id, tenantId: $tenant_id}}) RETURN n"
    params = {"node_id": node_id, "tenant_id": user_id}
    results_node = await asyncio.to_thread(client.query, query_node, user_id, params)
    assert len(results_node) == 1 and results_node[0] is not None, (
        f"Node with label '{label}' and id '{node_id}' not found in Neo4j for tenant {user_id}."
    )

    if expect_relationships:
        query_rels = f"MATCH (n:{label} {{id: $node_id, tenantId: $tenant_id}})-[r]-() RETURN count(r) as rel_count"
        results_rels = await asyncio.to_thread(client.query, query_rels, user_id, params)
        assert results_rels and results_rels[0]["rel_count"] > 0, (
            f"Node {node_id} was expected to have relationships but has none."
        )


# ==========================================================
# ===== INTEGRATION TESTS ==================================
# ==========================================================
class TestIntegrationIndexer:
    async def test_actions_collection_respects_targets(self, monkeypatch):
        """
        Tests that a new document is indexed correctly into the backends specified
        by the INGESTION_WORKER_MONGO_INDEXER_TARGETS setting, and NOT into others.
        This test will run 4 times, once for each configuration.
        """
        async with isolated_test_environment(monkeypatch) as env:
            indexer, db = env["indexer"], env["db"]

            # --- READ THE SETTINGS FROM THE TEST ENVIRONMENT ---
            targets_for_actions = indexer.settings.INGESTION_WORKER_MONGO_INDEXER_TARGETS.get(
                "Actions", []
            )
            logger.info(
                f"Verifying 'Actions' collection against configured targets: {targets_for_actions}"
            )

            action_doc = _generate_doc(BASE_ACTION, 10)
            await db["Actions"].insert_one(action_doc)

            await indexer._process_collection_updates(Context(), "Actions")

            # --- ASSERT (now conditional based on the mocked setting) ---
            state = await indexer.state_crud.get_state("Actions")
            assert state is not None, "State should have been created after processing."
            assert state.last_processed_cursor_id == str(action_doc["_id"])

            # Conditionally check Weaviate
            if "weaviate" in targets_for_actions:
                await assert_indexed(indexer, action_doc["user_id"], action_doc, "Actions")
            else:
                await assert_not_indexed(indexer, action_doc["user_id"], action_doc, "Actions")

            # Conditionally check Neo4j
            if "neo4j" in targets_for_actions:
                await assert_graph_indexed(action_doc["user_id"], action_doc)
            else:
                await assert_graph_not_indexed(action_doc["user_id"], action_doc)

    async def test_event_results_action_run_gets_rich_graph(self, monkeypatch):
        """
        Tests that a text-heavy 'action_run' EventResult is indexed and a rich graph
        with relationships is created via the GraphRAGProcessor.
        """
        async with isolated_test_environment(monkeypatch) as env:
            indexer, db = env["indexer"], env["db"]
            targets_for_events = indexer.settings.INGESTION_WORKER_MONGO_INDEXER_TARGETS.get(
                "EventResults", []
            )

            event_doc = _generate_doc(BASE_EVENT_RESULT_ACTION_RUN, 5)
            await db["EventResults"].insert_one(event_doc)

            await indexer._process_collection_updates(Context(), "EventResults")

            # For a simple action_run, the doc_id for Weaviate is just the _id
            weaviate_check_doc = event_doc

            if "weaviate" in targets_for_events:
                await assert_indexed(
                    indexer, event_doc["user_id"], weaviate_check_doc, "EventResults"
                )

            if "neo4j" in targets_for_events:
                # Assert that the main 'ExecutionRun' node was created AND has relationships.
                # FIX: Calculate the canonical UUID that the transformer now generates.
                expected_uuid = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{event_doc['user_id']}:EventResults:{event_doc['_id']}",
                    )
                )
                await assert_graph_indexed(
                    user_id=event_doc["user_id"],
                    mongo_doc=event_doc,
                    label="Chunk",
                    expect_relationships=True,
                    # Pass the correct, canonical UUID to the assertion helper.
                    node_id_override=expected_uuid,
                )

    async def test_event_results_graph_run_gets_structural_graph(self, monkeypatch):
        """
        Tests that an 'action_graph_run' EventResult is indexed and its structural graph
        (nodes and NEXT relationships) is created.
        """
        async with isolated_test_environment(monkeypatch) as env:
            indexer, db = env["indexer"], env["db"]
            targets_for_events = indexer.settings.INGESTION_WORKER_MONGO_INDEXER_TARGETS.get(
                "EventResults", []
            )

            event_doc = _generate_doc(BASE_EVENT_RESULT_GRAPH_RUN, 5)
            await db["EventResults"].insert_one(event_doc)

            await indexer._process_collection_updates(Context(), "EventResults")

            # Check one of the sub-chunks for Weaviate indexing
            node1_turn0_chunk_doc = event_doc.copy()
            node1_turn0_chunk_doc["_id"] = f"{str(event_doc['_id'])}_node1_turn_0"

            if "weaviate" in targets_for_events:
                await assert_indexed(
                    indexer, event_doc["user_id"], node1_turn0_chunk_doc, "EventResults"
                )

            if "neo4j" in targets_for_events:
                # For a graph run, we also expect relationships (HAS_STEP, NEXT, etc.)
                # The root node's ID must match the summary doc ID.
                # FIX: Calculate the canonical UUID for the summary node, matching the transformer's logic.
                summary_doc_id = f"{event_doc['_id']}_summary"
                summary_uuid = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{event_doc['user_id']}:EventResults:{summary_doc_id}",
                    )
                )
                await assert_graph_indexed(
                    user_id=event_doc["user_id"],
                    mongo_doc=event_doc,
                    label="Chunk",
                    expect_relationships=True,
                    # Pass the correct, canonical UUID for the summary node to the assertion helper.
                    node_id_override=summary_uuid,
                )

    async def test_event_results_graph_run_hydrates_from_s3(self, monkeypatch):
        """
        Tests that an 'action_graph_run' with a file_url correctly triggers
        the S3 hydration during an integration run and indexes the full data.
        """
        async with isolated_test_environment(monkeypatch) as env:
            indexer, db = env["indexer"], env["db"]
            targets_for_events = indexer.settings.INGESTION_WORKER_MONGO_INDEXER_TARGETS.get(
                "EventResults", []
            )

            # ARRANGE
            # 1. Create the "lite" document for MongoDB
            lite_mongo_doc = _generate_doc(LITE_GRAPH_RUN_FOR_MONGO_INTEGRATION, 5)

            # 2. Prepare the full data for S3, injecting the correct _id
            full_s3_data = FULL_GRAPH_RUN_FOR_S3_INTEGRATION.copy()
            full_s3_data["_id"] = str(lite_mongo_doc["_id"])

            # 3. Upload the "full" document to S3 using the real UserFiles helper
            from src.autobots.conn.aws.aws_s3 import get_s3
            from src.autobots.user.user_orm_model import UserORM

            test_user = UserORM(id=TEST_USER_ID_1)
            s3_client = get_s3()
            user_files = UserFiles(
                user=test_user, s3=s3_client, top_level_folder=indexer.settings.AWS_S3_RUNS_FOLDER
            )
            ctx = Context()
            s3_filename = f"{lite_mongo_doc['_id']}.json"
            file_metas = await user_files.upload_binary(
                ctx,
                file_obj=BytesIO(json.dumps(full_s3_data).encode("utf-8")),
                filename=s3_filename,
            )
            s3_url = str(file_metas[0].url)

            # 4. Insert the MongoDB doc, now including the real S3 URL
            await db["EventResults"].insert_one({**lite_mongo_doc, "file_url": s3_url})

            # ACT
            await indexer._process_collection_updates(Context(), "EventResults")

            # ASSERT
            if "weaviate" in targets_for_events:
                # Check that the text indexed comes from the S3 data.
                store = await indexer._get_weaviate_store(TEST_USER_ID_1)

                node1_chunk_doc_id = f"{lite_mongo_doc['_id']}_node1_turn_0"
                node1_deterministic_uuid = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL, f"{TEST_USER_ID_1}:EventResults:{node1_chunk_doc_id}"
                    )
                )

                results = await store.get(ids=[node1_deterministic_uuid])
                assert results and results[0] is not None, "Node1 chunk not found in Weaviate"

                # This is the crucial assertion: content MUST match S3 data.
                assert "Output of the first step from S3." in results[0].page_content

            # Clean up the created S3 file
            await user_files.delete_files(ctx, [s3_filename])

    async def test_action_graphs_collection_respects_targets(self, monkeypatch):
        """
        Tests that a new 'ActionGraphs' document is indexed into the backends specified
        by its settings.
        """
        async with isolated_test_environment(monkeypatch) as env:
            indexer, db = env["indexer"], env["db"]

            # --- READ THE SETTINGS FROM THE TEST ENVIRONMENT ---
            targets_for_graphs = indexer.settings.INGESTION_WORKER_MONGO_INDEXER_TARGETS.get(
                "ActionGraphs", []
            )
            logger.info(
                f"Verifying 'ActionGraphs' collection against configured targets: {targets_for_graphs}"
            )

            graph_doc = _generate_doc(BASE_GRAPH, 15)
            # Add minimal structure needed by the transformer
            graph_doc["node_details"] = {}
            graph_doc["graph"] = {}
            await db["ActionGraphs"].insert_one(graph_doc)

            await indexer._process_collection_updates(Context(), "ActionGraphs")

            # --- ASSERT ---
            state = await indexer.state_crud.get_state("ActionGraphs")
            assert state is not None
            assert state.last_processed_cursor_id == str(graph_doc["_id"])

            if "weaviate" in targets_for_graphs:
                await assert_indexed(indexer, graph_doc["user_id"], graph_doc, "ActionGraphs")
            else:
                await assert_not_indexed(indexer, graph_doc["user_id"], graph_doc, "ActionGraphs")

            if "neo4j" in targets_for_graphs:
                await assert_graph_indexed(graph_doc["user_id"], graph_doc)
            else:
                await assert_graph_not_indexed(graph_doc["user_id"], graph_doc)

    async def test_chats_collection_respects_targets(self, monkeypatch):
        """
        Tests that a new 'Chats' document is indexed into the backends specified
        by its settings.
        """
        async with isolated_test_environment(monkeypatch) as env:
            indexer, db = env["indexer"], env["db"]

            # --- READ THE SETTINGS FROM THE TEST ENVIRONMENT ---
            targets_for_chats = indexer.settings.INGESTION_WORKER_MONGO_INDEXER_TARGETS.get(
                "Chats", []
            )
            logger.info(
                f"Verifying 'Chats' collection against configured targets: {targets_for_chats}"
            )

            chat_doc = _generate_doc(BASE_CHAT, 2)
            await db["Chats"].insert_one(chat_doc)
            await indexer._process_collection_updates(Context(), "Chats")

            # --- ASSERT ---
            if "weaviate" in targets_for_chats:
                await assert_indexed(indexer, chat_doc["user_id"], chat_doc, "Chats")
            else:
                await assert_not_indexed(indexer, chat_doc["user_id"], chat_doc, "Chats")

    async def test_document_update_is_idempotent(self, monkeypatch):
        """Tests that processing the same document again (after an update) results in an upsert, not a duplicate."""
        async with isolated_test_environment(monkeypatch) as env:
            indexer, db = env["indexer"], env["db"]

            graph_doc = _generate_doc(BASE_GRAPH, 20)
            # Add minimal graph structure needed by the transformer
            graph_doc["node_details"] = {}
            graph_doc["graph"] = {}

            await db["ActionGraphs"].insert_one(graph_doc)
            await indexer._process_collection_updates(Context(), "ActionGraphs")

            # Update the document with a new timestamp to ensure it gets picked up again.
            new_desc = "Updated workflow description."
            new_ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None)
            await db["ActionGraphs"].update_one(
                {"_id": graph_doc["_id"]}, {"$set": {"description": new_desc, "updated_at": new_ts}}
            )
            await indexer._process_collection_updates(Context(), "ActionGraphs")

            # The assertion checks that the document (with its original ID) is still present.
            await assert_indexed(indexer, graph_doc["user_id"], graph_doc, "ActionGraphs")
            await assert_graph_indexed(graph_doc["user_id"], graph_doc)
            # We also expect the state to be advanced to the updated document's ID.
            state = await indexer.state_crud.get_state("ActionGraphs")
            assert state.last_processed_cursor_id == str(graph_doc["_id"])

    async def test_batch_processing_and_all_collections(self, monkeypatch):
        """Tests that the indexer can process multiple documents in a batch and handles all target collections."""
        async with isolated_test_environment(monkeypatch) as env:
            indexer, db = env["indexer"], env["db"]

            docs = {
                "Actions": [_generate_doc(BASE_ACTION, 5), _generate_doc(BASE_ACTION, 4)],
                "Chats": [_generate_doc(BASE_CHAT, 3), _generate_doc(BASE_CHAT, 2)],
            }
            await db["Actions"].insert_many(docs["Actions"])
            await db["Chats"].insert_many(docs["Chats"])

            # Run the indexer across all configured collections.
            await asyncio.gather(
                *[
                    indexer._process_collection_updates(Context(), name)
                    for name in indexer.settings.INGESTION_WORKER_MONGO_INDEXER_TARGETS.keys()
                ]
            )

            state_actions = await indexer.state_crud.get_state("Actions")
            assert state_actions is not None
            assert state_actions.last_processed_cursor_id == str(docs["Actions"][-1]["_id"])

            state_chats = await indexer.state_crud.get_state("Chats")
            assert state_chats is not None
            assert state_chats.last_processed_cursor_id == str(docs["Chats"][-1]["_id"])

    async def test_poison_pill_does_not_stall_pipeline(self, monkeypatch):
        """
        Tests that a malformed document is gracefully skipped without stalling the pipeline,
        and that subsequent valid documents are still processed.
        """
        async with isolated_test_environment(monkeypatch) as env:
            indexer, db = env["indexer"], env["db"]

            # Read the settings for this test run
            targets_for_actions = indexer.settings.INGESTION_WORKER_MONGO_INDEXER_TARGETS.get(
                "Actions", []
            )

            # --- ARRANGE (same as before) ---
            docs_to_insert = [
                _generate_doc(BASE_ACTION, 10),
                POISON_PILL,
                _generate_doc(BASE_ACTION, 8),
            ]
            docs_to_insert.sort(key=lambda d: d["updated_at"])
            valid_before = docs_to_insert[0]
            valid_after = docs_to_insert[2]

            await db["Actions"].insert_many(docs_to_insert)

            # --- ACT (same as before) ---
            await indexer._process_collection_updates(Context(), "Actions")

            # --- ASSERT (now conditional) ---
            state = await indexer.state_crud.get_state("Actions")
            assert state is not None
            # The cursor MUST advance past the poison pill to the last valid document.
            assert state.last_processed_cursor_id == str(valid_after["_id"])

            # Verify that the valid documents on BOTH sides of the pill were indexed correctly.
            if "weaviate" in targets_for_actions:
                await assert_indexed(indexer, valid_before["user_id"], valid_before, "Actions")
                await assert_indexed(indexer, valid_after["user_id"], valid_after, "Actions")
            else:
                await assert_not_indexed(indexer, valid_before["user_id"], valid_before, "Actions")
                await assert_not_indexed(indexer, valid_after["user_id"], valid_after, "Actions")

            if "neo4j" in targets_for_actions:
                await assert_graph_indexed(valid_before["user_id"], valid_before)
                await assert_graph_indexed(valid_after["user_id"], valid_after)
            else:
                await assert_graph_not_indexed(valid_before["user_id"], valid_before)
                await assert_graph_not_indexed(valid_after["user_id"], valid_after)

    async def test_indexer_handles_document_updates_idempotently(self, monkeypatch):
        """
        This test reproduces the idempotency bug. It should fail on the old code
        and pass once the Weaviate client uses an upsert/replace strategy.

        1. A document is created and indexed successfully.
        2. The same document is updated in MongoDB.
        3. The indexer runs again. On buggy code, this fails with a 422 error.
           On fixed code, it should succeed and update the document in Weaviate.
        """
        async with isolated_test_environment(monkeypatch) as env:
            indexer, db = env["indexer"], env["db"]

            # --- ARRANGE ---
            # 1. Create and insert the initial document
            original_doc = _generate_doc(BASE_ACTION, minutes_ago=10)
            await db["Actions"].insert_one(original_doc)

            # 2. Run the indexer once to perform the initial, successful insert
            print("--- Performing initial document insertion... ---")
            await indexer._process_collection_updates(Context(), "Actions")

            # 3. Verify it was inserted correctly the first time
            await assert_indexed(indexer, original_doc["user_id"], original_doc, "Actions")

            # 4. Update the document in MongoDB with a new description and timestamp
            updated_description = "This is the updated description."
            new_timestamp = datetime.utcnow().replace(tzinfo=None)
            await db["Actions"].update_one(
                {"_id": original_doc["_id"]},
                {"$set": {"description": updated_description, "updated_at": new_timestamp}},
            )
            print("--- MongoDB document updated. Performing re-indexing... ---")

            # --- ACT ---
            # 5. Run the indexer again. This is the step that will fail on the buggy code.
            await indexer._process_collection_updates(Context(), "Actions")

            # --- ASSERT ---
            # 6. The state cursor must have advanced to the updated document's ID.
            #    This proves the second run didn't get stuck and completed processing.
            state = await indexer.state_crud.get_state("Actions")
            assert state.last_processed_cursor_id == str(original_doc["_id"])

            # 7. Fetch the document from Weaviate and verify its content has been updated.
            store = await indexer._get_weaviate_store(original_doc["user_id"])
            # Use the deterministic UUID the application would generate
            deterministic_uuid = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL, f"{original_doc['user_id']}:Actions:{original_doc['_id']}"
                )
            )
            retrieved_docs = await store.get(ids=[deterministic_uuid])

            assert retrieved_docs and retrieved_docs[0] is not None, (
                "Document not found in Weaviate after update."
            )

            retrieved_page_content = retrieved_docs[0].page_content
            assert updated_description in retrieved_page_content, (
                "Document content in Weaviate was not updated."
            )
            assert BASE_ACTION["description"] not in retrieved_page_content, (
                "Old content should not be present."
            )

            print("--- Idempotency test passed: Document was successfully updated in Weaviate. ---")

    async def test_indexer_skips_collections_with_empty_targets(self, monkeypatch):
        """
        This test reproduces the 'stale knowledge base' issue by ensuring that if a
        collection's targets are an empty list, no data from it is loaded into any backend.
        """
        async with isolated_test_environment(monkeypatch) as env:
            indexer, db = env["indexer"], env["db"]

            # --- ARRANGE ---
            # 1. Override the settings to explicitly disable the 'Chats' collection
            test_settings = SettingsProvider.sget()
            test_settings.INGESTION_WORKER_MONGO_INDEXER_TARGETS["Chats"] = []  # Disable it
            monkeypatch.setattr(SettingsProvider, "_settings", test_settings)

            # 2. Insert a document into the now-disabled 'Chats' collection
            chat_doc = _generate_doc(BASE_CHAT, minutes_ago=5)
            await db["Chats"].insert_one(chat_doc)

            # --- ACT ---
            # 3. Run the indexer for the 'Chats' collection
            await indexer._process_collection_updates(Context(), "Chats")

            # --- ASSERT ---
            # 4. The cursor SHOULD still advance.
            state = await indexer.state_crud.get_state("Chats")
            assert state.last_processed_cursor_id == str(chat_doc["_id"]), (
                "Cursor failed to advance for disabled collection."
            )

            # 5. Assert that the document was NOT indexed into Weaviate or Neo4j.
            await assert_not_indexed(indexer, chat_doc["user_id"], chat_doc, "Chats")
            await assert_graph_not_indexed(chat_doc["user_id"], chat_doc)
