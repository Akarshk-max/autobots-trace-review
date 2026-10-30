# src/autobots/api/v1_ingestion/api_knowledge_base.py

import asyncio
import json
import random
import uuid
from collections import Counter, defaultdict
from typing import Any, Dict, List, Literal, Optional, Union
from urllib.parse import urlparse

import supabase_auth
import tiktoken
from anthropic.types import TextBlock
from anthropic.types.beta import BetaTextBlock
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from google.genai import types as gemini_types
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field, HttpUrl
from starlette.requests import Request

from src.autobots.action.action.common_action_models import (
    DataBlock,
    DataBlocks,
    GraphRAGSearchData,
    SourceChunk,
)
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.conn.claude.chat_model import ChatReqClaude
from src.autobots.conn.gemini.message_normalizer_gemini import GeminiMessageNormalizer
from src.autobots.conn.neo4j.interface import GraphDBInterface
from src.autobots.conn.neo4j.visualization_utils import (
    convert_neo4j_to_vis_json,
    generate_html_visualization,
)
from src.autobots.conn.object_storage.factory import get_public_storage
from src.autobots.conn.openai.openai_responses.openai_responses import LlmResponsesOpenaiConfig
from src.autobots.conn.openrouter.chat_model import OpenRouterChatReq
from src.autobots.conn.openrouter.openrouter_chat import OpenRouterChat
from src.autobots.conn.weaviate.weaviate_store import WeaviateStore
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.redis.async_pool import get_async_redis
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.ingestion.ingestion_llm_connector import (
    IngestionLLMConnector,
    NoActiveIngestionProvider,
)
from src.autobots.ingestion.job_status import JobStatusCreate, JobStatusCRUD
from src.autobots.ingestion.neo4j_loader import Neo4jManager
from src.autobots.ingestion.url_import_utils import import_public_url_to_public_s3_and_stage_for_kb
from src.autobots.knowledge_base.kb_crud import KnowledgeBaseCRUD
from src.autobots.knowledge_base.kb_doc_model import KnowledgeBase, KnowledgeBaseRole
from src.autobots.knowledge_base.kb_service import resolve_tenant_id_and_check_access
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.token_usage.token_usage_doc_model import TokenUsageSource
from src.autobots.token_usage.token_usage_service import LlmSurface, TokenUsageService
from src.autobots.user.user_orm_model import UserORM

# Uncomment for tests only
# from src.autobots.api.deps import get_current_user # For test
# from typing import Optional # For test

# --- Constants for Document Ranking ---
DOC_RANKING_ALPHA = 0.7  # Weight for best-score in ranking. 0.7 = 70% best, 30% average.


router = APIRouter(prefix="/knowledge-base", tags=["Knowledge Base RAG"])

try:
    TOKENIZER = tiktoken.encoding_for_model("gpt-4o")
except KeyError:
    TOKENIZER = tiktoken.get_encoding("cl100k_base")

MAX_DOCUMENT_CONTEXT_TOKENS = 8000


async def _resolve_rag_provider_or_503(
    ctx: Context,
    user: UserORM,
    db: AsyncIOMotorDatabase,
) -> tuple[str, str]:
    """Resolve the GraphRAG synthesis provider, returning a 503 when none is configured."""
    try:
        provider, model_name = await IngestionLLMConnector.resolve_provider(ctx, user, db)
    except NoActiveIngestionProvider:
        logger.bind(ctx=ctx).warning(
            "No LLM provider is available for GraphRAG synthesis"
        )
        raise HTTPException(
            status_code=503,
            detail="No LLM provider is currently available for this request.",
        )
    logger.bind(ctx=ctx, provider=provider, model_name=model_name).info(
        "GraphRAG query provider resolved for this request"
    )
    return provider, model_name


def _format_graph_context(records: List[Dict[str, Any]]) -> str:
    """Formats the raw graph query result into a human-readable string for the LLM."""
    if not records:
        return "No relational context found in the knowledge graph."

    # Using sets to ensure uniqueness
    nodes = {}
    rels = set()
    for record in records:
        # Process nodes from the 'nodes' list in the record
        if "nodes" in record and record["nodes"]:
            for node_data in record["nodes"]:
                if node_data and "id" in node_data and node_data["id"] not in nodes:
                    props = {
                        k: v for k, v in node_data.get("props", {}).items() if k not in ["text"]
                    }
                    nodes[node_data["id"]] = (
                        f"- Node '{node_data['id']}' (Type: {node_data.get('label', 'Unknown')}) has properties: {props}"
                    )

        # Process relationships from the 'rels' list
        if "rels" in record and record["rels"]:
            for rel_data in record["rels"]:
                if rel_data and all(
                    k in rel_data for k in ["start_node_id", "end_node_id", "type"]
                ):
                    rels.add(
                        f"- Relationship: ({rel_data['start_node_id']})-[{rel_data['type']}]->({rel_data['end_node_id']})"
                    )

    if not nodes and not rels:
        return "No structured nodes or relationships were found in the graph for the retrieved context."

    node_str = "\n".join(nodes.values())
    rel_str = "\n".join(sorted(list(rels)))
    return f"Entities and Concepts:\n{node_str}\n\nRelationships:\n{rel_str}"


def _safe_page_to_int(page_val: Union[str, int, float, None]) -> Union[float, tuple]:
    """
    Converts a page identifier to a sortable numeric value, handling various formats.
    Sort order: Special pages -> Roman numerals -> Numeric pages -> Unknown formats.
    """
    if page_val is None:
        return (float("inf"),)  # Sort missing values last

    if isinstance(page_val, (int, float)):
        return (1, float(page_val))  # Numeric group, value

    if isinstance(page_val, str):
        try:
            return (1, float(page_val))  # Numeric string
        except (ValueError, TypeError):
            pass

        # Handle Roman numerals (common in PDF front matter)
        roman_map = {
            "i": 1,
            "ii": 2,
            "iii": 3,
            "iv": 4,
            "v": 5,
            "vi": 6,
            "vii": 7,
            "viii": 8,
            "ix": 9,
            "x": 10,
        }
        lower_val = page_val.lower().strip()
        if lower_val in roman_map:
            return (0, roman_map[lower_val])  # Roman numeral group, value

        # For non-numeric strings (e.g., "cover", "toc"), sort lexicographically
        # but after all numeric pages.
        return (2, page_val)  # Text group, value

    return (float("inf"),)  # Fallback for other types


def _format_document_context(
    documents_data: Dict[str, Dict[str, Any]], sorted_doc_ids: List[str]
) -> str:
    """Formats the full document chunks into a structured string for the LLM, with token truncation."""
    if not documents_data:
        return "No full-text documents were retrieved for additional context."

    context_parts = []
    for doc_id in sorted_doc_ids:
        if doc_id in documents_data:
            doc_info = documents_data[doc_id]
            filename = doc_info.get("filename", "Unknown File")
            s3_url = doc_info.get("s3_url", "URL not available")

            header = f"--- Document: {filename} (Source: {s3_url}) ---\n"
            # Sort chunks deterministically. Prioritize the 'order' property for new data.
            # Fall back to 'page_number' and then 'page_content' for older data that lacks 'order'.
            # Using float('inf') ensures chunks without a property are sorted last.
            sorted_chunks = sorted(
                doc_info.get("chunks", []),
                key=lambda x: (
                    (x.metadata or {}).get("order", float("inf")),  # Primary: 'order' (int)
                    _safe_page_to_int(
                        (x.metadata or {}).get("page_number")
                    ),  # Secondary: Smart page sort
                    x.page_content,  # Tertiary: content for stability
                ),
            )

            chunk_texts = [chunk.page_content for chunk in sorted_chunks]

            full_text = header + "\n\n".join(chunk_texts)
            context_parts.append(full_text)

    final_context_str = "\n\n".join(context_parts)

    # Token Truncation Logic
    tokens = TOKENIZER.encode(final_context_str)
    if len(tokens) > MAX_DOCUMENT_CONTEXT_TOKENS:
        truncated_tokens = tokens[:MAX_DOCUMENT_CONTEXT_TOKENS]
        final_context_str = TOKENIZER.decode(truncated_tokens)
        final_context_str += "\n\n[... The rest of the document content has been truncated to fit the context boundary. ...]"
        logger.warning(
            f"Document context was truncated from {len(tokens)} to {len(truncated_tokens)} tokens."
        )

    return final_context_str


async def _generate_rag_answer(
    ctx: Context,
    app_state: AppState,
    user: UserORM,
    db: AsyncIOMotorDatabase,
    provider: str,
    model_name: str,
    prompt: str,
) -> str:
    generated_text = None

    if provider in ("azure", "openai"):
        client = await IngestionLLMConnector.get_openai_client(ctx, user, db, provider)
        responses_req = LlmResponsesOpenaiConfig(
            provider=provider,
            model=model_name,
            input=[{"role": "user", "content": prompt}],
            tools=[],
            store=True,
            stream=False,
        )
        response = await client.openai_responses.create(
            responses_req=responses_req,
            app_state=app_state,
            user=user,
            ctx=ctx,
            db=db,
        )
        generated_text = response.output_text if response else None
    elif provider == "gemini":
        client = await IngestionLLMConnector.get_gemini_client(ctx, user, db)
        response = await client.generate_content(
            model=model_name,
            contents=prompt,
            config=gemini_types.GenerateContentConfig(temperature=0.0),
        )
        TokenUsageService.record(
            ctx=ctx,
            surface=LlmSurface.gemini,
            app_state=app_state,
            usage=getattr(response, "usage_metadata", None),
            model=model_name,
            loop=1,
        )
        candidate = response.candidates[0] if response and response.candidates else None
        parts = candidate.content.parts if candidate and candidate.content else None
        generated_text = GeminiMessageNormalizer.extract_text_from_parts(parts) if parts else None
    elif provider == "anthropic":
        client = await IngestionLLMConnector.get_claude_client(ctx, user, db)
        chat_res = await client.claude_chat.chat(
            user=user,
            ctx=ctx,
            app_state=app_state,
            chat_req=ChatReqClaude(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                stream=False,
                temperature=0.0,
            ),
            db=db,
        )
        if chat_res and chat_res.content:
            generated_text = "".join(
                block.text
                for block in chat_res.content
                if isinstance(block, (TextBlock, BetaTextBlock)) and block.text
            )
    elif provider == "openrouter":
        connection = await IngestionLLMConnector.get_openrouter_client(ctx, user, db)
        chat_res = await OpenRouterChat(client=connection.client).chat(
            app_state=app_state,
            chat_req=OpenRouterChatReq(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                stream=False,
            ),
            user=user,
            ctx=ctx,
            db=db,
        )
        generated_text = (
            chat_res.choices[-1].message.content if chat_res and chat_res.choices else None
        )
    else:
        raise NotImplementedError(f"Provider '{provider}' is not supported for RAG synthesis.")

    return (
        generated_text.strip()
        if generated_text and generated_text.strip()
        else "Could not generate a response."
    )


async def _get_rag_data(
    ctx: Context,
    app_state: AppState,
    user: UserORM,
    db: AsyncIOMotorDatabase,
    query: str,
    collection_name: str,
    vector_search_top_k: int,
    graph_traversal_depth: int,
    docs_context_top_k: int,
    provider: str,
    model_name: str,
    tenant_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Internal service function that preserves the original, complex RAG logic
    while packaging the results into a structured dictionary.
    """
    # Backwards-compatible default: if no tenant_id is provided, use the user's
    # personal knowledge base (one-tenant-per-user model).
    #
    # Callers that know kb_id should pass the resolved tenant_id explicitly.
    if tenant_id is None:
        tenant_id = str(user.id)
    sources = []  # Define sources list here

    app_state = app_state.child_context(
        source=TokenUsageSource.knowledge_base,
        source_id=tenant_id,
    )
    TokenUsageService.start_collection(app_state)
    rag_status = EventResultStatus.success

    try:
        # --- Stage 1: Vector Search ---
        weaviate_store = await WeaviateStore.create_store(
            collection_name=collection_name, user_id=tenant_id, skip_init_checks=True
        )

        # Over-fetch to ensure document diversity
        fetch_k = max(20, vector_search_top_k * 4)
        logger.info(f"Over-fetching {fetch_k} chunks to ensure document diversity.")

        retrieved_chunks_with_scores = []
        for attempt in range(3):  # Retry up to 3 times
            retrieved_chunks_with_scores = await weaviate_store.similarity_search_with_score(
                query, k=fetch_k
            )
            if retrieved_chunks_with_scores:
                logger.info(f"RAG service vector search successful on attempt {attempt + 1}.")
                break  # Success, exit the loop
            logger.warning(
                f"RAG service search attempt {attempt + 1} found no results for query '{query}'. Retrying in 1s..."
            )
            await asyncio.sleep(1)  # A short wait for the index to catch up.

        if not retrieved_chunks_with_scores:
            await weaviate_store.close()
            # New Return Format: Return a structured dictionary instead of a DataBlocks object.
            return {
                "answer": "I couldn't find any directly relevant information.",
                "sources": [],
                "graph_data": [],
            }

        # --- REFACTORED LOGIC: Create both `sources` and `retrieved_chunks` in one pass ---
        retrieved_chunks = []
        chunk_node_ids = []
        for doc, score in retrieved_chunks_with_scores:
            retrieved_chunks.append(doc)
            # --- DEBUG LOG 3: Log the UUID retrieved from Weaviate ---
            chunk_id_from_weaviate = doc.metadata.get("chunk_id")
            logger.debug(
                f"[UUID TRACE 3/3 - WEAVIATE QUERY] Retrieved chunk_uuid from Weaviate: {chunk_id_from_weaviate}"
            )

            # First, create a complete data dictionary
            source_data = {
                **doc.metadata,
                "text_preview": doc.page_content,
                "score": 1.0 - score if score is not None else 0.0,
            }

            # --- FIX: Ensure chunk_id is a string before validation ---
            # Pydantic's Document model might auto-coerce the ID to a UUID object.
            # The SourceChunk model strictly expects a string. We cast it here to ensure compatibility.
            if "chunk_id" in source_data and isinstance(source_data["chunk_id"], uuid.UUID):
                source_data["chunk_id"] = str(source_data["chunk_id"])

            # --- ROBUST FIX: Standardize the UUID regardless of its input type ---
            # This handles if it's a string OR already a UUID object.
            if isinstance(chunk_id_from_weaviate, uuid.UUID):
                # If it's already a UUID object, just convert it to a string
                chunk_node_ids.append(str(chunk_id_from_weaviate))
            else:
                # If it's a string (hyphenated or not), normalize it through the constructor
                chunk_node_ids.append(str(uuid.UUID(chunk_id_from_weaviate)))
            # Then, validate it to create the model instance
            sources.append(SourceChunk.model_validate(source_data))

        # --- Document Selection and Full Context Retrieval ---

        # 1. Group all valid chunk scores by their document_id.
        doc_scores = defaultdict(list)
        for s in sources:
            # Defensive check: only include chunks that have a document_id and a valid score.
            if s.document_id and s.score is not None:
                doc_scores[s.document_id].append(s.score)

        # 2. If valid scores were found, use the robust weighted-score ranking.
        if doc_scores:
            doc_final_scores = {}
            for doc_id, scores in doc_scores.items():
                if not scores:  # Should not happen with the check above, but as a safeguard.
                    continue
                max_score = max(scores)
                avg_score = sum(scores) / len(scores)
                doc_final_scores[doc_id] = (
                    DOC_RANKING_ALPHA * max_score + (1 - DOC_RANKING_ALPHA) * avg_score
                )

            sorted_doc_ids = sorted(doc_final_scores, key=doc_final_scores.get, reverse=True)
        else:
            # 3. FALLBACK: If no valid scores exist, revert to the original frequency-based ranking.
            logger.warning(
                "No valid scores found for any chunks. Falling back to frequency-based document ranking."
            )
            doc_id_counts = Counter(s.document_id for s in sources if s.document_id)
            sorted_doc_ids = [doc_id for doc_id, count in doc_id_counts.most_common()]

        selected_doc_ids = sorted_doc_ids[:docs_context_top_k]

        document_context_str = "No full-text documents were retrieved for additional context."
        if selected_doc_ids and docs_context_top_k > 0:
            # 2. Fetch all chunks for the selected documents using a reliable filter-only method
            logger.info(
                f"Fetching full document context for {len(selected_doc_ids)} document(s)..."
            )
            all_doc_chunks = await weaviate_store.fetch_objects_by_filter(
                filter_dict={"document_id__in": selected_doc_ids}, limit=1000
            )

            # 3. Group chunks by document
            documents_data = {}
            for chunk in all_doc_chunks:
                doc_id = chunk.metadata.get("document_id")
                if not doc_id:
                    continue
                if doc_id not in documents_data:
                    documents_data[doc_id] = {
                        "filename": chunk.metadata.get("source"),
                        "s3_url": chunk.metadata.get("s3_url"),
                        "chunks": [],
                    }
                documents_data[doc_id]["chunks"].append(chunk)

            # 4. Format into the third context string
            document_context_str = _format_document_context(documents_data, selected_doc_ids)

        # Trim the initial chunks and sources back to the original requested k for the semantic context
        retrieved_chunks_with_scores = retrieved_chunks_with_scores[:vector_search_top_k]
        sources = sources[:vector_search_top_k]
        retrieved_chunks = retrieved_chunks[:vector_search_top_k]
        chunk_node_ids = chunk_node_ids[:vector_search_top_k]

        # All Weaviate operations are done, so we can close the connection.
        await weaviate_store.close()

        if not chunk_node_ids:
            return {
                "answer": "Found text, but could not link it to the knowledge graph.",
                "sources": [s.model_dump(by_alias=True) for s in sources],
                "graph_data": [],
            }

        # --- Stage 2: Graph Traversal ---
        neo4j_client: GraphDBInterface = await Neo4jManager.get_client()

        # --- FIX: Explicitly cast all chunk IDs to strings ---
        # This guarantees that the neo4j-driver receives a list of strings,
        # preventing the "uuid.UUID not supported" serialization error.
        chunk_ids_as_strings = [str(cid) for cid in chunk_node_ids]
        query_params = {"chunk_ids": chunk_ids_as_strings, "tenantId": tenant_id}

        # This is the V2 query. It uses native Cypher `reduce()` to flatten lists,
        # making it robust and free of external (APOC) dependencies. It is also
        # syntactically correct for modern Neo4j versions, resolving all bugs.
        cypher_query = f"""
        // ── Stage-1: bring starting chunks and the tenantId variable into scope ─────────────
        MATCH (c:Chunk) WHERE c.id IN $chunk_ids AND c.tenantId = $tenantId
        // Create a `tenantId` variable from the parameter BEFORE the CALL
        WITH c, $tenantId AS tenantId
        
        // ── Stage-2: bounded traversal inside a sub-query ───────────────────────────
        // Now, import the existing `c` and `tenantId` variables
        CALL {{
          WITH c, tenantId
          OPTIONAL MATCH path = (c)-[*..{graph_traversal_depth}]-(other)
                WHERE path IS NULL OR all(n IN nodes(path) WHERE n.tenantId = tenantId)
          RETURN
            collect(DISTINCT nodes(path))      AS node_lists,
            collect(DISTINCT relationships(path)) AS rel_lists,
            c                                  AS start_chunk
          LIMIT 20
        }}
        
        // ── Stage-3: flatten collections without losing the row on empty lists ──────
        WITH
          node_lists  + [[start_chunk]]                       AS raw_node_lists,
          rel_lists                                           AS raw_rel_lists
        
        WITH
          reduce(acc = [], nl IN raw_node_lists  | acc + nl)  AS all_nodes_raw,
          reduce(acc = [], rl IN raw_rel_lists | acc + rl)    AS all_rels_raw
        
        WITH
          [n IN all_nodes_raw WHERE n IS NOT NULL]            AS all_nodes,
          [r IN all_rels_raw  WHERE r IS NOT NULL]            AS all_rels
        
        RETURN
          [n IN all_nodes |
               {{id: n.id, label: labels(n)[0], props: properties(n)}}]               AS nodes,
          [r IN all_rels  |
               {{start_node_id: startNode(r).id,
                 end_node_id:   endNode(r).id,
                 type:          type(r)}}]                                            AS rels
        """
        # This enhanced retry loop handles eventual consistency with more patience and
        # exponential backoff. It is the primary defense against replication lag.
        graph_context_records = []
        max_attempts = 6  # Total wait time: ~1+2+4+8+16 ≈ 31 seconds
        for attempt in range(max_attempts):
            graph_context_records = await asyncio.to_thread(
                neo4j_client.query, cypher_query, tenant_id, query_params
            )
            # The query always returns one record. Success is when its 'nodes' list is not empty.
            if graph_context_records and graph_context_records[0].get("nodes"):
                logger.info(f"Neo4j graph traversal successful on attempt {attempt + 1}.")
                break
            if attempt < max_attempts - 1:
                # Exponential backoff: 1s, 2s, 4s, 8s, 16s...
                wait_time = 2**attempt + random.uniform(0, 0.5)
                logger.warning(
                    f"Graph traversal returned no nodes on attempt {attempt + 1}. "
                    f"Retrying in {wait_time:.2f}s for eventual consistency..."
                )
                await asyncio.sleep(wait_time)

        # --- Stage 3: LLM Synthesis ---
        vector_context = "\n".join([f"- {chunk.page_content}" for chunk in retrieved_chunks])
        graph_context_str = _format_graph_context(graph_context_records)

        final_prompt = f"""
        You are an expert research assistant. Synthesize a comprehensive answer to the user's question using the provided context.
        ### User's Question:
        {query}
        ---
        ### Context 1: Semantic Search (Top Chunks)
        {vector_context}
        ---
        ### Context 2: Knowledge Graph
        {graph_context_str}
        ---
        ### Context 3: Full-Text Source Documents
        {document_context_str}
         ---
        ### INSTRUCTIONS:
        - Base your answer on ALL the provided information from all three contexts.
        - Prioritize information from the Full-Text Source Documents (Context 3) for accuracy.
        - Use the Semantic Search Snippets (Context 1) and Knowledge Graph (Context 2) to understand focus and relationships.
        - If the information is insufficient, state that clearly.
        """

        # --- VERIFICATION LOG: Display the full final prompt being sent to the LLM ---
        logger.bind(ctx=ctx).info(
            f"--- Final RAG Prompt to LLM ---\n{final_prompt}\n---------------------------------"
        )

        # Use the new IngestionLLMConnector to get the correct client
        final_answer = await _generate_rag_answer(
            ctx=ctx,
            app_state=app_state,
            user=user,
            db=db,
            provider=provider,
            model_name=model_name,
            prompt=final_prompt,
        )

        return {
            "answer": final_answer,
            "sources": [s.model_dump(by_alias=True) for s in sources],
            "graph_data": graph_context_records,
        }

    except Exception as e:
        rag_status = EventResultStatus.error
        logger.bind(ctx=ctx).exception(f"An error occurred during RAG search: {e}")
        return {
            "answer": "An internal error occurred during the search process.",
            "sources": [],
            "graph_data": [],
        }
    finally:
        await TokenUsageService.flush(
            ctx=ctx,
            app_state=app_state,
            user=user,
            db=db,
            status=rag_status,
        )


# A simple Pydantic model for the search request body
class SearchRequest(BaseModel):
    query: str
    collection_name: str
    vector_search_top_k: int = 20
    graph_traversal_depth: int = 1
    docs_context_top_k: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Number of full source documents to include for context (0-10).",
    )
    kb_id: Optional[str] = None
    scope: Optional[Literal["personal", "kb", "all"]] = None


@router.post(
    "/search", response_model=DataBlocks, summary="Perform a RAG search on the knowledge base."
)
async def perform_rag_search(
    request: Request,
    request_body: SearchRequest,
    user_res: supabase_auth.UserResponse = Depends(
        get_offline_user_from_access_token
    ),  # Use for production, comment for tests
    # _test_user: Optional[UserORM] = Depends(get_current_user), # Uncomment for tests only
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> DataBlocks:
    """
    Performs a full RAG query using Weaviate and Neo4j for a given user.
    This is the new, centralized home for the RAG logic.
    This endpoint returns structured JSON data for a frontend application to render.
    Behavior:
      - If kb_id is provided: search a single KB (or personal KB resolved by RBAC),
        with graph-only secondary fallback for visualization.
      - If kb_id is omitted: search across all accessible KBs (personal + shared),
        and run graph-only fallback per KB when needed, then fuse answers.
    """
    ctx = request.state.context
    app_state = request.state.app_state
    # user = _test_user # Uncomment for tests only
    user = UserORM(
        **user_res.user.model_dump(), space_member=space_member
    )  # Use for production, comment for tests

    # --- Gap 1: Determine Effective Scope ---
    # Backwards compatibility: If scope is missing, infer it from kb_id presence.
    effective_scope = request_body.scope
    if effective_scope is None:
        if request_body.kb_id:
            effective_scope = "kb"
        else:
            effective_scope = "all"  # Default to Fan-out

    # --- Branch A: Single-Tenant Search (Personal or Specific Shared KB) ---
    if effective_scope in ("personal", "kb"):
        target_tenant_id = None

        if effective_scope == "personal":
            target_tenant_id = str(user.id)
        else:  # scope == "kb"
            if not request_body.kb_id:
                raise HTTPException(status_code=400, detail="kb_id is required when scope is 'kb'")

            target_tenant_id = await resolve_tenant_id_and_check_access(
                user=user,
                db=db,
                kb_id=request_body.kb_id,
                required_role=KnowledgeBaseRole.READ,
                ctx=ctx,
            )

        # Resolve credentials only after request validation and KB authorization.
        provider, model_name = await _resolve_rag_provider_or_503(ctx, user, db)

        logger.bind(ctx=ctx).info(
            f"Received scoped RAG search request (scope={effective_scope}) for collection "
            f"'{request_body.collection_name}' (kb_id={request_body.kb_id}, tenant_id={target_tenant_id})"
        )

        rag_results = await _get_rag_data(
            ctx,
            app_state,
            user,
            db,
            query=request_body.query,
            collection_name=request_body.collection_name,
            vector_search_top_k=request_body.vector_search_top_k,
            graph_traversal_depth=request_body.graph_traversal_depth,
            docs_context_top_k=request_body.docs_context_top_k,
            provider=provider,
            model_name=model_name,
            tenant_id=target_tenant_id,
        )

        # --- GRACEFUL FALLBACK: extra Neo4j traversal for this single KB ---
        graph_data = rag_results.get("graph_data", [])
        if not graph_data or not graph_data[0].get("nodes"):
            logger.warning(
                "Primary RAG call returned no graph data. Initiating fallback retry loop..."
            )

            source_chunks = rag_results.get("sources")
            if source_chunks:
                chunk_ids_for_fallback = [chunk["chunk_id"] for chunk in source_chunks]
                try:
                    neo4j_client: GraphDBInterface = await Neo4jManager.get_client()

                    fallback_query = f"""
                    // ── Stage-1: bring starting chunks and the tenantId variable into scope ─────────────
                    MATCH (c:Chunk) WHERE c.id IN $chunk_ids AND c.tenantId = $tenantId
                    // Create a `tenantId` variable from the parameter BEFORE the CALL
                    WITH c, $tenantId AS tenantId

                    // ── Stage-2: bounded traversal inside a sub-query ───────────────────────────
                    // Now, import the existing `c` and `tenantId` variables
                    CALL {{
                      WITH c, tenantId
                      OPTIONAL MATCH path = (c)-[*..{request_body.graph_traversal_depth}]-(other)
                            WHERE path IS NULL OR all(n IN nodes(path) WHERE n.tenantId = tenantId)
                      RETURN
                        collect(DISTINCT nodes(path))      AS node_lists,
                        collect(DISTINCT relationships(path)) AS rel_lists,
                        c                                  AS start_chunk
                      LIMIT 20
                    }}

                    // ── Stage-3: flatten collections without losing the row on empty lists ──────
                    WITH
                      node_lists  + [[start_chunk]]                       AS raw_node_lists,
                      rel_lists                                           AS raw_rel_lists

                    WITH
                      reduce(acc = [], nl IN raw_node_lists  | acc + nl)  AS all_nodes_raw,
                      reduce(acc = [], rl IN raw_rel_lists | acc + rl)    AS all_rels_raw

                    WITH
                      [n IN all_nodes_raw WHERE n IS NOT NULL]            AS all_nodes,
                      [r IN all_rels_raw  WHERE r IS NOT NULL]            AS all_rels

                    RETURN
                      [n IN all_nodes |
                           {{id: n.id, label: labels(n)[0], props: properties(n)}}] AS nodes,
                      [r IN all_rels  |
                           {{start_node_id: startNode(r).id,
                             end_node_id:   endNode(r).id,
                             type:          type(r)}}] AS rels
                    """
                    fallback_params = {
                        "chunk_ids": chunk_ids_for_fallback,
                        "tenantId": target_tenant_id,
                    }

                    # The retry loop for the fallback query (5 attempts as before)
                    for attempt in range(5):  # 5 attempts
                        wait_time = 2**attempt  # 1s, 2s, 4s, 8s, 16s
                        logger.info(
                            f"Executing fallback graph query (Attempt {attempt + 1}/5 after {wait_time}s wait)..."
                        )
                        await asyncio.sleep(wait_time)
                        fallback_records = await asyncio.to_thread(
                            neo4j_client.query, fallback_query, target_tenant_id, fallback_params
                        )
                        if fallback_records and fallback_records[0].get("nodes"):
                            logger.success(
                                "Fallback graph query successful. Populating visualization."
                            )
                            rag_results["graph_data"] = fallback_records
                            break  # Exit the retry loop on success

                except Exception as e:
                    logger.exception(f"An error occurred during the fallback graph query: {e}")

        source_objects = [SourceChunk.model_validate(s) for s in rag_results.get("sources", [])]
        vis_json = None
        if rag_results.get("graph_data"):
            vis_json_data = convert_neo4j_to_vis_json(rag_results["graph_data"])
            if vis_json_data and (vis_json_data.get("nodes") or vis_json_data.get("edges")):
                vis_json = vis_json_data

        search_data_object = None
        if source_objects or vis_json:
            search_data_object = GraphRAGSearchData(
                sources=source_objects, graph_visualization=vis_json
            )

        single_block = DataBlock(text=rag_results["answer"], search_data=search_data_object)
        final_response = DataBlocks(blocks=[single_block])

        logger.bind(ctx=ctx, final_response=final_response.model_dump(mode="json")).info(
            "Final RAG response constructed with all datablocks (single-KB)."
        )

        return final_response

    # --- Branch B: Multi-KB Search (Fan-out) ---
    # effective_scope == "all"
    logger.bind(ctx=ctx).info(
        f"Received multi-KB RAG search request for collection '{request_body.collection_name}' "
        f"(kb_id=None => all accessible KBs)."
    )

    kb_crud = KnowledgeBaseCRUD(db)

    # Personal KB / tenant
    tenant_entries: List[Dict[str, Any]] = [
        {
            "kb_id": None,
            "name": "Personal KB",
            "tenant_id": str(user.id),
        }
    ]

    # Shared KBs where user is owner or member
    owner_kbs: List[KnowledgeBase] = await kb_crud.list_for_owner(str(user.id))
    member_docs = (
        await db["KnowledgeBaseMemberships"].find({"user_id": str(user.id)}).to_list(length=None)
    )
    member_kb_ids = {doc["kb_id"] for doc in member_docs}

    member_kbs: List[KnowledgeBase] = []
    if member_kb_ids:
        extra_docs = (
            await db["KnowledgeBases"]
            .find({"_id": {"$in": list(member_kb_ids)}})
            .to_list(length=None)
        )
        member_kbs = [KnowledgeBase.model_validate(d) for d in extra_docs]

    kb_by_id = {kb.id: kb for kb in owner_kbs + member_kbs}

    for kb in kb_by_id.values():
        tenant_entries.append({"kb_id": kb.id, "name": kb.name, "tenant_id": kb.tenant_id})

    if not tenant_entries:
        return DataBlocks(
            blocks=[
                DataBlock(
                    text=(
                        "You don't have access to any knowledge bases yet. "
                        "Please create or join a knowledge base first."
                    ),
                    search_data=None,
                )
            ]
        )

    # At this point the fan-out target set has been derived from the authenticated user.
    provider, model_name = await _resolve_rag_provider_or_503(ctx, user, db)

    # Limit concurrency across KBs to avoid pathological fanout.
    MAX_CONCURRENT_KB_SEARCHES = 5
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_KB_SEARCHES)

    async def _search_single_kb(entry: Dict[str, Any]) -> Dict[str, Any] | Exception:
        async with semaphore:
            return await _get_rag_data(
                ctx,
                app_state,
                user,
                db,
                query=request_body.query,
                collection_name=request_body.collection_name,
                vector_search_top_k=request_body.vector_search_top_k,
                graph_traversal_depth=request_body.graph_traversal_depth,
                docs_context_top_k=request_body.docs_context_top_k,
                provider=provider,
                model_name=model_name,
                tenant_id=entry["tenant_id"],
            )

    tasks = [_search_single_kb(entry) for entry in tenant_entries]
    results_list = await asyncio.gather(*tasks, return_exceptions=True)

    multi_results: List[Dict[str, Any]] = []
    for i, result in enumerate(results_list):
        entry = tenant_entries[i]
        if isinstance(result, Exception):
            logger.bind(ctx=ctx).error(
                f"RAG search failed for KB '{entry['name']}' (tenant={entry['tenant_id']}): {result}"
            )
            continue
        multi_results.append(
            {"kb_name": entry["name"], "tenant_id": entry["tenant_id"], "rag": result}
        )

    # --- Secondary graph-only fallback per KB (for visualization) ---
    if multi_results:
        try:
            neo4j_client: GraphDBInterface = await Neo4jManager.get_client()
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Failed to get Neo4j client for multi-KB fallback: {e}")
            neo4j_client = None

        if neo4j_client:
            for entry in multi_results:
                tenant_for_kb = entry["tenant_id"]
                rag = entry["rag"]
                graph_data = rag.get("graph_data") or []
                if graph_data and graph_data[0].get("nodes"):
                    continue  # already have graph
                sources = rag.get("sources") or []
                if not sources:
                    continue

                chunk_ids_for_fallback = [s["chunk_id"] for s in sources if s.get("chunk_id")]
                if not chunk_ids_for_fallback:
                    continue

                try:
                    fallback_query = f"""
                    // ── Stage-1: bring starting chunks and the tenantId variable into scope ─────────────
                    MATCH (c:Chunk) WHERE c.id IN $chunk_ids AND c.tenantId = $tenantId
                    // Create a `tenantId` variable from the parameter BEFORE the CALL
                    WITH c, $tenantId AS tenantId

                    // ── Stage-2: bounded traversal inside a sub-query ───────────────────────────
                    // Now, import the existing `c` and `tenantId` variables
                    CALL {{
                      WITH c, tenantId
                      OPTIONAL MATCH path = (c)-[*..{request_body.graph_traversal_depth}]-(other)
                            WHERE path IS NULL OR all(n IN nodes(path) WHERE n.tenantId = tenantId)
                      RETURN
                        collect(DISTINCT nodes(path))      AS node_lists,
                        collect(DISTINCT relationships(path)) AS rel_lists,
                        c                                  AS start_chunk
                      LIMIT 20
                    }}

                    // ── Stage-3: flatten collections without losing the row on empty lists ──────
                    WITH
                      node_lists  + [[start_chunk]]                       AS raw_node_lists,
                      rel_lists                                           AS raw_rel_lists

                    WITH
                      reduce(acc = [], nl IN raw_node_lists  | acc + nl)  AS all_nodes_raw,
                      reduce(acc = [], rl IN raw_rel_lists | acc + rl)    AS all_rels_raw

                    WITH
                      [n IN all_nodes_raw WHERE n IS NOT NULL]            AS all_nodes,
                      [r IN all_rels_raw  WHERE r IS NOT NULL]            AS all_rels

                    RETURN
                      [n IN all_nodes |
                           {{id: n.id, label: labels(n)[0], props: properties(n)}}] AS nodes,
                      [r IN all_rels  |
                           {{start_node_id: startNode(r).id,
                             end_node_id:   endNode(r).id,
                             type:          type(r)}}] AS rels
                    """
                    fallback_params = {
                        "chunk_ids": chunk_ids_for_fallback,
                        "tenantId": tenant_for_kb,
                    }

                    for attempt in range(5):
                        wait_time = 2**attempt
                        logger.bind(ctx=ctx).info(
                            f"[Multi-KB Fallback] Executing fallback graph query for tenant "
                            f"{tenant_for_kb} (Attempt {attempt + 1}/5 after {wait_time}s wait)..."
                        )
                        await asyncio.sleep(wait_time)
                        fallback_records = await asyncio.to_thread(
                            neo4j_client.query, fallback_query, tenant_for_kb, fallback_params
                        )
                        if fallback_records and fallback_records[0].get("nodes"):
                            logger.bind(ctx=ctx).success(
                                f"[Multi-KB Fallback] Fallback graph query successful for tenant "
                                f"{tenant_for_kb}. Populating visualization."
                            )
                            rag["graph_data"] = fallback_records
                            break

                except Exception as e:
                    logger.bind(ctx=ctx).exception(
                        f"[Multi-KB Fallback] Error while running fallback graph query for "
                        f"tenant {tenant_for_kb}: {e}"
                    )

    # Aggregate answers via a second-level LLM summarisation
    answers_sections = []
    for res in multi_results:
        kb_name = res["kb_name"]
        answer = res["rag"].get("answer") or ""
        if answer.strip():
            answers_sections.append(f"### Answer from KB '{kb_name}':\n{answer}")

    combined_answers_text = "\n\n".join(answers_sections) if answers_sections else "No answers."

    final_prompt = f"""
You are an expert assistant. Multiple knowledge bases were searched for the user's question.

User's Question:
{request_body.query}

Answers from individual knowledge bases:
{combined_answers_text}

Please synthesise a single, coherent answer that best addresses the user's question,
using and reconciling the information from all the provided answers. If information
is conflicting, note that explicitly. If there is not enough information to answer,
say so clearly.
"""

    fusion_app_state = app_state.child_context(
        source=TokenUsageSource.knowledge_base, source_id=str(user.id)
    )
    TokenUsageService.start_collection(fusion_app_state)
    fusion_status = EventResultStatus.success
    try:
        aggregated_answer = await _generate_rag_answer(
            ctx=ctx,
            app_state=fusion_app_state,
            user=user,
            db=db,
            provider=provider,
            model_name=model_name,
            prompt=final_prompt,
        )
    except Exception:
        fusion_status = EventResultStatus.error
        raise
    finally:
        await TokenUsageService.flush(
            ctx=ctx, app_state=fusion_app_state, user=user, db=db, status=fusion_status
        )

    # Aggregate sources and graph data across KBs
    all_sources_raw: List[Dict[str, Any]] = []
    all_graph_data: List[Dict[str, Any]] = []
    for res in multi_results:
        rag = res["rag"]
        all_sources_raw.extend(rag.get("sources") or [])
        all_graph_data.extend(rag.get("graph_data") or [])

    source_objects = [SourceChunk.model_validate(s) for s in all_sources_raw]

    vis_json = None
    if all_graph_data:
        vis_json_data = convert_neo4j_to_vis_json(all_graph_data)
        if vis_json_data and (vis_json_data.get("nodes") or vis_json_data.get("edges")):
            vis_json = vis_json_data

    search_data_object = None
    if source_objects or vis_json:
        search_data_object = GraphRAGSearchData(
            sources=source_objects, graph_visualization=vis_json
        )

    multi_block = DataBlock(text=aggregated_answer, search_data=search_data_object)
    final_response = DataBlocks(blocks=[multi_block])

    logger.bind(ctx=ctx, final_response=final_response.model_dump(mode="json")).info(
        "Final multi-KB RAG response constructed with all datablocks."
    )

    return final_response


@router.post(
    "/search/visualize",
    response_class=HTMLResponse,
    summary="[HTML] Renders a Graph RAG visualization.",
    include_in_schema=False,
)
async def visualize_rag_search(
    request: Request,
    request_body: SearchRequest,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """
    A utility endpoint that returns a self-contained, interactive HTML page
    for debugging and demonstrating the knowledge graph.
    """
    ctx = request.state.context
    app_state = request.state.app_state
    user = UserORM(**user_res.user.model_dump(), space_member=space_member)
    tenant_id = await resolve_tenant_id_and_check_access(
        user=user,
        db=db,
        kb_id=request_body.kb_id,
        required_role=KnowledgeBaseRole.READ,
        ctx=ctx,
    )
    provider, model_name = await _resolve_rag_provider_or_503(ctx, user, db)
    rag_results = await _get_rag_data(
        ctx,
        app_state,
        user,
        db,
        query=request_body.query,
        collection_name=request_body.collection_name,
        vector_search_top_k=request_body.vector_search_top_k,
        graph_traversal_depth=request_body.graph_traversal_depth,
        docs_context_top_k=request_body.docs_context_top_k,
        provider=provider,
        model_name=model_name,
        tenant_id=tenant_id,
    )

    if not rag_results.get("graph_data"):
        return HTMLResponse(
            "<html><body><h1>No graph data found for this query.</h1></body></html>",
            status_code=200,
        )

    html_content = generate_html_visualization(rag_results["graph_data"], return_html=True)

    headers = {"X-Content-Type-Options": "nosniff"}
    return HTMLResponse(content=html_content, status_code=200, headers=headers)


class IngestS3URLRequest(BaseModel):
    s3_url: HttpUrl
    # Backwards compatible: callers may still send original_filename.
    # If omitted, we derive it from the URL path (querystring removed).
    original_filename: Optional[str] = None
    collection_name: str = "synced_files"
    targets: List[Literal["weaviate", "graphrag"]] = ["weaviate", "graphrag"]
    kb_id: Optional[str] = None


@router.post(
    "/ingest-from-s3-url", summary="Queue ingestion from a public S3 URL.", status_code=202
)
async def ingest_file_from_s3_url(
    request: Request,
    request_body: IngestS3URLRequest,
    user_res: supabase_auth.UserResponse = Depends(
        get_offline_user_from_access_token
    ),  # Use for production, comment for tests
    # _test_user: Optional[UserORM] = Depends(get_current_user), # Uncomment for tests only
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    ctx = request.state.context
    # user = _test_user # Uncomment for tests only
    user = UserORM(
        **user_res.user.model_dump(), space_member=space_member
    )  # Use for production, comment for tests

    # Determine effective tenant and enforce KB WRITE access. Backwards compatible:
    #   - If kb_id is None, tenant_id = user.id (personal KB).
    tenant_id = await resolve_tenant_id_and_check_access(
        user=user,
        db=db,
        kb_id=request_body.kb_id,
        required_role=KnowledgeBaseRole.READ_WRITE,
        ctx=ctx,
    )

    input_url = str(request_body.s3_url)
    logger.bind(ctx=ctx).info(
        f"Received ingestion request for URL: {input_url} "
        f"(kb_id={request_body.kb_id}, tenant_id={tenant_id})"
    )

    job_id = str(uuid.uuid4())
    job_crud = JobStatusCRUD(db)

    # Note: Only create the job status document with the initial QUEUED state.
    # Do not update it to AVAILABLE here. The background worker is solely
    # responsible for managing the lifecycle from this point forward. This
    # change is the primary fix for the race condition observed in the E2E test.
    # The Action's polling loop will now correctly wait for the full
    # ingestion-and-probing cycle to complete before proceeding with the query.

    # UserFiles applies the user prefix before uploading imported files.
    public_s3 = get_public_storage()

    # If the URL already points to our public object-storage container:
    # - derive key from URL path
    # - do NOT download/re-upload
    in_bucket_key = await public_s3.get_key_if_owned_url(input_url)

    original_filename = request_body.original_filename
    persisted_s3_url: str
    persisted_s3_key: str
    s3_key_for_worker: str

    # If client did not send a filename OR sent a suspicious one, infer from URL.
    if (
        not original_filename
        or ("?" in original_filename)
        or ("/" in original_filename)
        or ("\\" in original_filename)
    ):
        from src.autobots.files.user_files import UserFiles

        original_filename = await UserFiles.extract_file_name(ctx=ctx, file_url=input_url)

    # Ensure we have an extension for worker suffix detection. Keep behavior minimal:
    # do not rewrite spaces/etc for in-bucket URLs; just enforce "has extension".
    if "." not in original_filename or original_filename.endswith("."):
        raise HTTPException(
            status_code=400, detail="original_filename must include a valid extension."
        )

    if in_bucket_key:
        # Unchanged path for "our S3" URLs, but canonicalize only if URL has query/fragment.
        parsed = urlparse(input_url)
        if parsed.query or parsed.fragment:
            persisted_s3_url = str(await public_s3.get_https_url(in_bucket_key))
        else:
            persisted_s3_url = input_url
        persisted_s3_key = in_bucket_key
        s3_key_for_worker = in_bucket_key
    else:
        # Non-S3 URL (or other bucket): import into our S3 first, then stage for worker
        imported = await import_public_url_to_public_s3_and_stage_for_kb(
            ctx=ctx,
            user=user,
            public_s3=public_s3,
            remote_url=input_url,
            job_id=job_id,
            original_filename=original_filename,
        )
        original_filename = imported.original_filename
        persisted_s3_url = imported.persisted_s3_url
        persisted_s3_key = imported.persisted_s3_key
        s3_key_for_worker = imported.staging_s3_key

    # Create job record only AFTER S3 ops succeed (prevents ghost jobs)
    await job_crud.create(
        JobStatusCreate(
            _id=job_id,
            user_id=str(user.id),
            kb_id=request_body.kb_id,
            tenant_id=tenant_id,
            uploaded_by_user_id=str(user.id),
            uploaded_by_email=user.email,
            collection_name=request_body.collection_name,
            original_filename=original_filename,
            targets=request_body.targets,
            # Store the GOOD S3 URL (persisted for public imports; original for S3)
            s3_url=persisted_s3_url,
        )
    )

    job_payload = {
        "job_id": job_id,
        "user_id": str(user.id),
        "kb_id": request_body.kb_id,
        "tenant_id": tenant_id,
        "s3_key": s3_key_for_worker,
        "s3_url": persisted_s3_url,
        "persisted_s3_key": persisted_s3_key,
        "persisted_s3_url": persisted_s3_url,
        "source_url": input_url,
        "collection_name": request_body.collection_name,
        "original_filename": original_filename,
        "job_type": "high_priority",
        "targets": request_body.targets,
    }

    try:
        settings = SettingsProvider.sget()
        r = await get_async_redis()
        await r.lpush(
            settings.REDIS_INGESTION_HIGH_PRIORITY_QUEUE,
            json.dumps(job_payload),
        )
        logger.bind(ctx=ctx).info(f"Queued ingestion job for URL {input_url}")
        return {"message": "URL accepted and queued for ingestion.", "job_id": job_id}
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Failed to queue ingestion job for URL {input_url}: {e}")
        raise HTTPException(status_code=500, detail="Failed to queue ingestion job.")
