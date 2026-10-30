# src/autobots/ingestion/graphrag_processor.py
import asyncio
import json
import json.decoder
import re
from datetime import datetime
from functools import partial
from typing import Any, Dict, List, Optional

import tiktoken
from anthropic.types import TextBlock
from anthropic.types.beta import BetaTextBlock
from google.genai import types as gemini_types
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.chroma.chroma import Document
from src.autobots.conn.claude.chat_model import ChatReqClaude
from src.autobots.conn.gemini.inline_json_schema import fix_const_fields, strip_unsupported_keys
from src.autobots.conn.gemini.message_normalizer_gemini import GeminiMessageNormalizer
from src.autobots.conn.neo4j.ontology import ChunkNode, ExtractedGraph, get_ontology_json_string
from src.autobots.conn.openai.openai_responses.openai_responses import LlmResponsesOpenaiConfig
from src.autobots.conn.openrouter.chat_model import OpenRouterChatReq
from src.autobots.conn.openrouter.openrouter_chat import OpenRouterChat
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.ingestion.abstractions import IngestionProcessor
from src.autobots.ingestion.ingestion_llm_connector import IngestionLLMConnector
from src.autobots.token_usage.token_usage_doc_model import TokenUsageSource
from src.autobots.token_usage.token_usage_service import LlmSurface, TokenUsageService
from src.autobots.user.user_orm_model import UserORM

# --- Constants for Batching Logic ---
# The model_config determines which provider and model the LLM dispatcher will use.
# It follows a '<provider>/<model_name>' format.
#
# --- Available Model Options ---
#
# 1. OpenAI / Azure (Responses API — structured output via text.format.json_schema):
#    - "openai/gpt-5.6-terra"
#    - "openai/gpt-5.6-luna"
#    - "openai/gpt-5.6-sol"
#    - "openai/gpt-5.5"
#    - "openai/gpt-5.4"
#    - "openai/gpt-5.2"
#    - "openai/gpt-5.1"
#    - "openai/gpt-5"
#    - "azure/gpt-5.6-terra"
#    - "azure/gpt-5.2"
#    - (See `openai_responses_models.py` for the full RESPONSES_MODELS list)
#
# 2. OpenRouter (Chat Completions — structured output via response_format json_schema):
#    - "openrouter/<model>" (e.g. "openrouter/openai/gpt-5.6-terra")
#    - (See `openrouter/model_capabilities.py` for available models)
#
# 3. Gemini (Google GenAI SDK — structured output via response_mime_type + response_json_schema):
#    - "gemini/gemini-3.5-flash"
#    - "gemini/gemini-3.1-pro-preview"
#    - "gemini/gemini-2.5-pro"
#    - "gemini/gemini-2.5-flash"
#    - (See `gemini/model_registry_gemini.py`'s `GeminiModelRegistry` for all versions)
#
# 4. Anthropic (Claude — prompt-based JSON extraction, no native JSON mode):
#    - "anthropic/claude-sonnet-5"
#    - "anthropic/claude-opus-4-6"
#    - "anthropic/claude-sonnet-4-6"
#    - (See `claude/chat_model.py`'s `ClaudeModelEnum` class for all versions)
#
# Note: Ensure that the API keys and endpoints for the chosen provider are correctly
# configured in your .env.local file.

DEFAULT_MODEL_CONFIG = "auto"  # Auto-select provider based on user's active connections

# Default model for each provider, used when model_config is "auto" or just "<provider>".
PROVIDER_DEFAULT_MODELS = {
    "openai": "gpt-5.6-terra",
    "azure": "gpt-5.6-terra",
    "openrouter": "openai/gpt-5.6-terra",
    "gemini": "gemini-3.5-flash",
    "anthropic": "claude-sonnet-5",
}

CONTEXT_WINDOW_LIMIT = 16000  # Use a much smaller, safer window for this complex task
SAFE_TOKEN_MARGIN = 0.90  # We can be a bit more aggressive with the margin now
MAX_CONCURRENT_REQUESTS = 5  # Limit concurrent API calls to avoid rate limiting

try:
    TOKENIZER = tiktoken.encoding_for_model("gpt-4o")  # Use a common tokenizer for estimation
except KeyError:
    TOKENIZER = tiktoken.get_encoding("cl100k_base")


def safe_before_sleep_log(logger, level):
    """A tenacity `before_sleep` callback that safely logs exceptions using loguru's exc_info."""

    def log_it(retry_state):
        # Log only pickle-safe primitives, not the function object or the full exception.
        if retry_state.outcome.failed:
            exception = retry_state.outcome.exception()
            logger.log(
                level,
                "Retrying function {func_name} in {sleep:.2f}s due to: {exc_type} - {exc_msg}",
                func_name=retry_state.fn.__name__,
                sleep=retry_state.next_action.sleep,
                exc_type=type(exception).__name__,
                exc_msg=str(exception),
            )

    return log_it


class GraphValidationException(ValueError):
    """Custom exception raised when LLM output fails logical consistency checks."""

    pass


class TokenUsageAnalyzer:
    """A helper class to record and summarize token usage for each LLM call."""

    TOKEN_THRESHOLD = 8000  # Define a threshold to flag potentially problematic batches

    def __init__(self):
        self.usage_records = []
        self._tokenizer = TOKENIZER

    def record_batch_usage(
        self,
        batch: List[Dict],
        instructional_prompt: str,
        llm_response_str: Optional[str],
        status: str,
        error_message: Optional[str] = None,
    ):
        """Calculates and records the token usage for a single batch."""
        # Accurately reflect the true boilerplate by including the schema tokens.
        instructional_tokens = len(self._tokenizer.encode(instructional_prompt))
        schema_tokens = len(self._tokenizer.encode(get_ontology_json_string()))
        boilerplate_tokens = instructional_tokens + schema_tokens
        response_tokens = len(self._tokenizer.encode(llm_response_str)) if llm_response_str else 0

        chunk_details = []
        total_chunk_tokens = 0
        for chunk in batch:
            chunk_tokens = len(self._tokenizer.encode(chunk["text"]))
            chunk_details.append(
                {
                    "chunk_id": chunk["chunk_id"],
                    "tokens": chunk_tokens,
                    "text_excerpt": chunk["text"][:100] + "...",
                }
            )
            total_chunk_tokens += chunk_tokens

        total_input_tokens = boilerplate_tokens + total_chunk_tokens
        total_tokens = total_input_tokens + response_tokens

        self.usage_records.append(
            {
                "status": status,
                "error": error_message,
                "total_tokens": total_tokens,
                "total_input": total_input_tokens,
                "total_output": response_tokens,
                "boilerplate_tokens": boilerplate_tokens,
                "chunks": chunk_details,
            }
        )

    def print_summary(self):
        """Prints a detailed summary of all recorded API calls and flags issues using a context-bound logger."""
        summary_lines = ["\n" + "=" * 80, "LLM TOKEN USAGE ANALYSIS SUMMARY", "=" * 80]

        for i, record in enumerate(self.usage_records):
            status_marker = "SUCCESS" if record["status"] == "SUCCESS" else "FAILURE"
            summary_lines.append(f"\n--- Batch #{i + 1} ---")
            summary_lines.append(f"Status: {status_marker}")
            if record["error"]:
                summary_lines.append(f"Error: {record['error']}")

            summary_lines.append("Token Breakdown:")
            summary_lines.append(f"  - Total Tokens (Input+Output): {record['total_tokens']}")
            summary_lines.append(
                f"  - Boilerplate (Instructions + Schema): {record['boilerplate_tokens']} tokens"
            )
            summary_lines.append(
                f"  - Total for Chunks: {sum(c['tokens'] for c in record['chunks'])} tokens"
            )
            summary_lines.append(f"  - LLM Response (JSON Output): {record['total_output']} tokens")
            summary_lines.append("  Chunks in this Batch:")
            for chunk in record["chunks"]:
                summary_lines.append(
                    f"    - Chunk ID {chunk['chunk_id']}: {chunk['tokens']} tokens"
                )

        logger.info("\n".join(summary_lines))

        analysis_lines = [
            "\n" + "=" * 80,
            "ANALYSIS OF POTENTIALLY PROBLEMATIC CHUNKS",
            f"(Flagging batches with total tokens > {self.TOKEN_THRESHOLD})",
            "=" * 80,
        ]

        found_issues = False
        for i, record in enumerate(self.usage_records):
            if record["total_tokens"] > self.TOKEN_THRESHOLD or record["status"] == "FAILURE":
                found_issues = True
                analysis_lines.append(
                    f"\n[!] Problem detected in Batch #{i + 1} (Total Tokens: {record['total_tokens']})"
                )
                for chunk in record["chunks"]:
                    analysis_lines.append(f"  - Chunk ID: {chunk['chunk_id']}")
                    analysis_lines.append(f"  - Token Count: {chunk['tokens']}")
                    analysis_lines.append(f"  - Text Excerpt: '{chunk['text_excerpt']}'")

        if not found_issues:
            analysis_lines.append(
                "\nNo batches exceeded the defined token threshold. All failures were likely due to other issues (e.g., validation)."
            )

        logger.info("\n".join(analysis_lines))


class NoOpAnalyzer:
    """A dummy analyzer that does nothing, adhering to the Null Object Pattern."""

    def record_batch_usage(self, *args, **kwargs):
        pass

    def print_summary(self, *args, **kwargs):
        pass


def _validate_graph_response_shape(parsed_response: Any, batch: List[Dict]) -> None:
    if not isinstance(parsed_response, dict):
        raise GraphValidationException("Response root must be an object.")

    results_list = parsed_response.get("results")
    if not isinstance(results_list, list):
        raise GraphValidationException("'results' must be a list.")

    expected_ids = {str(chunk["chunk_id"]) for chunk in batch}
    actual_ids: list[str] = []

    for result in results_list:
        if not isinstance(result, dict):
            raise GraphValidationException("Every result must be an object.")
        if "chunk_id" not in result:
            raise GraphValidationException("Every result must contain 'chunk_id'.")

        result_chunk_id = str(result["chunk_id"])
        actual_ids.append(result_chunk_id)
        graph_data = result.get("graph")
        if not isinstance(graph_data, dict):
            raise GraphValidationException(
                f"'graph' for chunk {result_chunk_id} must be an object."
            )

        nodes = graph_data.get("nodes", [])
        relationships = graph_data.get("relationships", [])
        if not isinstance(nodes, list):
            raise GraphValidationException(f"'nodes' for chunk {result_chunk_id} must be a list.")
        if not isinstance(relationships, list):
            raise GraphValidationException(
                f"'relationships' for chunk {result_chunk_id} must be a list."
            )

        raw_node_ids: set[str] = set()
        chunk_node_count = 0
        for node in nodes:
            if not isinstance(node, dict) or "id" not in node:
                raise GraphValidationException(
                    "Every node must be an object containing an 'id'."
                )

            node_id = str(node["id"])
            if node_id in raw_node_ids:
                raise GraphValidationException(
                    f"Duplicate raw node ID '{node_id}' in chunk {result_chunk_id}."
                )
            raw_node_ids.add(node_id)
            if node.get("label") == "Chunk":
                chunk_node_count += 1

        if chunk_node_count > 1:
            raise GraphValidationException(
                f"Chunk {result_chunk_id} contains multiple ChunkNodes."
            )

        if not all(isinstance(relationship, dict) for relationship in relationships):
            raise GraphValidationException("Every relationship must be an object.")

    if len(actual_ids) != len(set(actual_ids)):
        raise GraphValidationException("LLM returned duplicate chunk IDs.")
    if set(actual_ids) != expected_ids:
        raise GraphValidationException(
            "Result chunk IDs do not match the batch. "
            f"Expected {sorted(expected_ids)}, got {sorted(set(actual_ids))}."
        )


def _normalize_graph_response(parsed_response: Dict, batch: List[Dict]) -> Dict[str, Dict]:
    batch_map = {str(chunk["chunk_id"]): chunk for chunk in batch}

    for result in parsed_response.get("results", []):
        if not isinstance(result, dict):
            continue

        canonical_chunk_id = str(result.get("chunk_id", -1))
        source_chunk = batch_map.get(canonical_chunk_id)
        graph_data = result.get("graph")
        if source_chunk is None or not isinstance(graph_data, dict):
            continue

        chunk_id_aliases = {canonical_chunk_id, f"chunk_{canonical_chunk_id}"}
        nodes = graph_data.get("nodes")
        if nodes is None:
            nodes = []
            graph_data["nodes"] = nodes
        relationships = graph_data.get("relationships", [])
        has_chunk_node = False
        if isinstance(nodes, list):
            for node in nodes:
                if not isinstance(node, dict) or node.get("label") != "Chunk":
                    continue

                has_chunk_node = True
                if node.get("id") is not None:
                    chunk_id_aliases.add(str(node["id"]))
                node["id"] = canonical_chunk_id
                node["text"] = source_chunk["text"]
                node["order"] = int(source_chunk["chunk_id"])

            has_graph_content = any(
                graph_data.get(field)
                for field in ("nodes", "relationships", "aliases", "party_groups")
            )
            if not has_chunk_node and has_graph_content:
                nodes.insert(
                    0,
                    {
                        "id": canonical_chunk_id,
                        "label": "Chunk",
                        "text": source_chunk["text"],
                        "chunk_type": "DocumentChunk",
                        "properties": {},
                        "order": int(source_chunk["chunk_id"]),
                    },
                )

        if isinstance(relationships, list):
            for relationship in relationships:
                if not isinstance(relationship, dict):
                    continue
                for endpoint in ("source_id", "target_id"):
                    value = relationship.get(endpoint)
                    if value is not None and str(value) in chunk_id_aliases:
                        relationship[endpoint] = canonical_chunk_id

    return batch_map


def _validate_graph_consistency(parsed_response: Dict, batch: List[Dict]):
    """
    Performs logical consistency checks on the LLM's graph output.
    Raises GraphValidationException if any validation fails, which will be caught by the retry decorator.
    """
    results_list = parsed_response.get("results", [])
    # Validation 1: Check if the LLM processed all chunks in the batch.
    # This is a critical instruction; failure to follow it makes the output unusable.
    if len(results_list) != len(batch):
        raise GraphValidationException(
            f"LLM did not process all chunks. Expected {len(batch)} results, but got {len(results_list)}."
        )

    # Validation 2: Check for dangling relationships within each extracted graph.
    for result in results_list:
        graph_data = result.get("graph")
        if not graph_data:
            continue  # Skip empty graphs, which are valid.

        nodes = graph_data.get("nodes", [])
        relationships = graph_data.get("relationships", [])

        canonical_node_ids = [str(node["id"]) for node in nodes]
        if len(canonical_node_ids) != len(set(canonical_node_ids)):
            raise GraphValidationException(
                f"Graph for chunk {result.get('chunk_id')} contains duplicate node IDs after normalization."
            )

        chunk_nodes = [node for node in nodes if node.get("label") == "Chunk"]
        has_graph_content = any(
            graph_data.get(field)
            for field in ("nodes", "relationships", "aliases", "party_groups")
        )
        if has_graph_content and len(chunk_nodes) != 1:
            raise GraphValidationException(
                f"Graph for chunk {result.get('chunk_id')} must contain exactly one ChunkNode; "
                f"got {len(chunk_nodes)}."
            )

        if not relationships:
            continue  # Nothing to validate if there are no relationships

        node_ids = set(canonical_node_ids)
        for i, rel in enumerate(relationships):
            if str(rel.get("source_id")) not in node_ids:
                raise GraphValidationException(
                    f"Dangling relationship at index {i}: source_id '{rel.get('source_id')}' does not exist in the nodes list."
                )
            if str(rel.get("target_id")) not in node_ids:
                raise GraphValidationException(
                    f"Dangling relationship at index {i}: target_id '{rel.get('target_id')}' does not exist in the nodes list."
                )


class GraphRAGProcessor(IngestionProcessor):
    def __init__(self, model_config: str = DEFAULT_MODEL_CONFIG, analyze_tokens: bool = False):
        self.model_config = model_config
        # Use the Null Object pattern to avoid 'if' checks in the logic
        self.token_analyzer = TokenUsageAnalyzer() if analyze_tokens else NoOpAnalyzer()
        logger.info(f"GraphRAGProcessor initialized with model config: '{self.model_config}'")

    @staticmethod
    async def _resolve_provider(
        ctx: Context, user: UserORM, db: AsyncIOMotorDatabase
    ) -> tuple[str, str]:
        """
        Auto-select the first available LLM provider based on the user's active connections.

        Priority order (best graph extraction quality first):
        1. OpenAI   — native structured outputs via Responses API
        2. Gemini   — native structured outputs via response_schema
        3. Claude   — prompt-based JSON extraction
        4. OpenRouter — system-level fallback (Chat Completions)

        Returns (provider, model_name) where provider is one of the keys in
        PROVIDER_DEFAULT_MODELS.
        """
        # 1. OpenAI (checks user ownership first, then system for runkai.ai domain)
        try:
            openai_conn = await AppConn.get_openai(
                ctx=ctx, db=db, user=user, provider="openai"
            )
            if not isinstance(openai_conn, Exception):
                logger.bind(ctx=ctx).info("Auto-select: using OpenAI for graphrag")
                return "openai", PROVIDER_DEFAULT_MODELS["openai"]
        except Exception as e:
            logger.bind(ctx=ctx, error=str(e)).debug("OpenAI not available for auto-select")

        # 2. Gemini
        try:
            gemini_conn = await AppConn.get_gemini(ctx=ctx, db=db, user=user)
            if not isinstance(gemini_conn, Exception):
                logger.bind(ctx=ctx).info("Auto-select: using Gemini for graphrag")
                return "gemini", PROVIDER_DEFAULT_MODELS["gemini"]
        except Exception as e:
            logger.bind(ctx=ctx, error=str(e)).debug("Gemini not available for auto-select")

        # 3. Claude
        try:
            claude_conn = await AppConn.get_claude(ctx=ctx, db=db, user=user)
            if not isinstance(claude_conn, Exception):
                logger.bind(ctx=ctx).info("Auto-select: using Claude for graphrag")
                return "anthropic", PROVIDER_DEFAULT_MODELS["anthropic"]
        except Exception as e:
            logger.bind(ctx=ctx, error=str(e)).debug("Claude not available for auto-select")

        # 4. OpenRouter
        try:
            openrouter_conn = await AppConn.get_openrouter(ctx=ctx, db=db, user=user)
            if not isinstance(openrouter_conn, Exception):
                logger.bind(ctx=ctx).info("Auto-select: using OpenRouter for graphrag")
                return "openrouter", PROVIDER_DEFAULT_MODELS["openrouter"]
        except Exception as e:
            logger.bind(ctx=ctx, error=str(e)).debug("OpenRouter not available for auto-select")

        raise RuntimeError(
            "No active LLM provider found for graphrag. "
            "Configure at least one provider (OpenAI, Gemini, Claude, or OpenRouter)."
        )

    async def process(
        self,
        elements: List[Document],
        metadata: Dict,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        ctx: Context,
        app_state: AppState,
    ) -> ExtractedGraph:
        document_app_state = app_state.child_context(
            source=TokenUsageSource.ingestion,
            source_id=metadata.get("document_id", "unknown_doc"),
        )
        TokenUsageService.start_collection(document_app_state)
        status = EventResultStatus.success
        try:
            return await self._process_document(
                elements=elements,
                metadata=metadata,
                user=user,
                db=db,
                ctx=ctx,
                app_state=document_app_state,
            )
        except Exception:
            status = EventResultStatus.error
            raise
        finally:
            await TokenUsageService.flush(
                ctx=ctx,
                app_state=document_app_state,
                user=user,
                db=db,
                status=status,
            )

    async def _process_document(
        self,
        elements: List[Document],
        metadata: Dict,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        ctx: Context,
        app_state: AppState,
    ) -> ExtractedGraph:
        document_id = metadata.get("document_id", "unknown_doc")
        final_graph = ExtractedGraph()

        try:
            # 1. Prepare chunks with token counts using simple sequential IDs for the LLM
            chunks_to_process = []
            for i, doc in enumerate(elements):
                chunks_to_process.append(
                    {
                        "chunk_id": i,  # Use a simple index for the LLM prompt
                        "text": doc.page_content,
                        "token_count": len(TOKENIZER.encode(doc.page_content)),
                    }
                )

            # 2. Create batches dynamically based on token limits
            batches = self._create_batches(chunks_to_process)

            # 2a. Resolve provider and model once for all batches.
            # If model_config is "auto", auto-select based on user's active connections.
            # Otherwise, parse "<provider>/<model_name>" from model_config.
            # If only "<provider>" is given (no "/"), use the default model for that provider.
            if self.model_config == "auto":
                provider, model_name = await self._resolve_provider(ctx, user, db)
            elif "/" in self.model_config:
                provider, model_name = self.model_config.split("/", 1)
            else:
                provider = self.model_config
                if provider not in PROVIDER_DEFAULT_MODELS:
                    raise ValueError(
                        f"Unknown provider '{provider}'. "
                        f"Valid providers: {list(PROVIDER_DEFAULT_MODELS.keys())}"
                    )
                model_name = PROVIDER_DEFAULT_MODELS[provider]
            logger.bind(
                ctx=ctx, provider=provider, model_name=model_name
            ).info("GraphRAG provider resolved for this run")

            # 3. Process batches concurrently with a semaphore
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

            # Use functools.partial to pass context and analyzer to each task
            task_function = partial(
                self._process_batch,
                user=user,
                db=db,
                ctx=ctx,
                app_state=app_state,
                token_analyzer=self.token_analyzer,
                provider=provider,
                model_name=model_name,
            )
            tasks = [task_function(batch, document_id, semaphore) for batch in batches]

            # DEBUG LOG #1: Gather results, including exceptions, to see what each task returned.
            # This prevents a single failing task from crashing the whole process and lets us inspect all outcomes.
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            logger.bind(batch_results=batch_results).debug(
                "--- Full Batch Results (including exceptions) ---"
            )
        finally:
            # This block is now guaranteed to run on success or failure of the process.
            logger.info("GraphRAGProcessor.process is finished. Printing token analysis summary...")
            self.token_analyzer.print_summary()

        # 4. Merge results from all batches into the final graph
        partial_graphs: list[ExtractedGraph] = []
        failed_batches = 0

        for i, result in enumerate(batch_results):
            if isinstance(result, list):
                # successful worker returns list[ExtractedGraph]
                partial_graphs.extend(result)
            elif isinstance(result, Exception):
                failed_batches += 1
                logger.error(f"Batch #{i + 1} failed and will be skipped. Error: {result}")

        logger.info(
            f"Graph extraction completed: {len(partial_graphs)} successful graphs from "
            f"{len(batches)} batches ({failed_batches} failed)."
        )

        MAX_FAILURE_RATE = 0.25  # 25 % tolerance
        if batches and (failed_batches / len(batches)) > MAX_FAILURE_RATE:
            raise RuntimeError(
                f"{failed_batches}/{len(batches)} graph-extraction batches "
                f"failed (>{MAX_FAILURE_RATE:.0%} tolerance). Aborting."
            )

        if failed_batches:
            logger.warning(
                f"Some batches failed ({failed_batches}/{len(batches)}). Resulting graph may be partial."
            )

        for partial_graph in partial_graphs:
            if partial_graph:
                self._merge_graphs(final_graph, partial_graph)

        self._normalize_data(final_graph)

        # DEBUG LOG #3: Log the final graph object before sending it to the loader.
        logger.bind(final_graph=final_graph.model_dump()).debug(
            "--- Final Graph Object to be Loaded ---"
        )

        logger.success(f"Successfully built and normalized graph for document '{document_id}'.")
        return final_graph

    def _create_batches(self, chunks: List[Dict]) -> List[List[Dict]]:
        """Dynamically creates batches of chunks that fit within the LLM's context window."""
        batches = []
        current_batch = []
        current_token_count = 0
        MAX_CHUNKS_PER_BATCH = (
            3  # Add a hard cap for reliability (small reliable cap for complex JSON extraction)
        )
        # Estimate the static prompt size (system message + ontology) once
        static_prompt_tokens = (
            len(TOKENIZER.encode(get_ontology_json_string())) + 500
        )  # Increase buffer for instructions

        for chunk in chunks:
            # If we've reached the chunk count cap, flush the current batch.
            if len(current_batch) >= MAX_CHUNKS_PER_BATCH:
                batches.append(current_batch)
                current_batch = []
                current_token_count = 0

            if not current_batch or (
                current_token_count + chunk["token_count"] + static_prompt_tokens
            ) <= (CONTEXT_WINDOW_LIMIT * SAFE_TOKEN_MARGIN):
                current_batch.append(chunk)
                current_token_count += chunk["token_count"]
            else:
                logger.info(
                    f"Batch created with {len(current_batch)} chunks and {current_token_count} tokens."
                )
                batches.append(current_batch)
                current_batch = [chunk]
                current_token_count = chunk["token_count"]

        if current_batch:
            batches.append(current_batch)

        logger.info(
            f"Final batch created with {len(current_batch)} chunks and {current_token_count} tokens."
        )
        logger.info(f"Created {len(batches)} batches from {len(chunks)} chunks.")
        return batches

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=5, max=30),  # A more patient backoff for LLM APIs
        retry=retry_if_exception_type(
            (json.decoder.JSONDecodeError, ValidationError, GraphValidationException)
        ),
        before_sleep=safe_before_sleep_log(logger, "WARNING"),
    )
    async def _process_batch(
        self,
        batch: List[Dict],
        document_id: str,
        semaphore: asyncio.Semaphore,
        # Accept the full context and analyzer
        user: UserORM,
        db: AsyncIOMotorDatabase,
        ctx: Context,
        app_state: AppState,
        token_analyzer: TokenUsageAnalyzer,
        # Provider/model resolved once in process() and passed down to all batches.
        provider: str = "openai",
        model_name: str = PROVIDER_DEFAULT_MODELS["openai"],
    ) -> List[Optional[ExtractedGraph]]:
        """Processes a single batch of chunks by calling the central LLM dispatcher."""
        async with semaphore:
            logger.debug(f"Processing batch of {len(batch)} chunks for document '{document_id}'.")
            if not batch:
                return []

            # Prepare the list of chunks for the prompt
            chunks_for_prompt = [{"chunk_id": c["chunk_id"], "text": c["text"]} for c in batch]

            # This prompt is now provider-agnostic. It no longer contains the schema.
            # Added more explicit instructions to improve reliability.
            # Define the instructional part of the prompt for accurate token counting.
            instructional_prompt = f"""
            Analyze the provided list of text chunks from document '{document_id}'.

            IMPORTANT INSTRUCTIONS:
            - For 'EntityNode', the 'label' MUST be one of ["Person", "Organization", "Location", "LegalTerm", "Concept"].
            - For 'ChunkNode', the 'label' MUST be exactly "Chunk".
            - For every EntityNode you extract, you MUST also create a "MENTIONS" relationship from the chunk_id where it was found to the entity's id.
            - All 'id', 'source_id', and 'target_id' fields MUST be string values enclosed in double quotes.

            **CRITICAL**: Every non-empty graph MUST contain exactly one `ChunkNode` for its input chunk. Never create a relationship from a chunk without including that `ChunkNode`. Every `ChunkNode` MUST include the 'text', 'chunk_type', and 'order' fields. The 'text' field MUST contain the original input chunk text. Do not omit these fields.

            For each chunk provided in the input, extract a knowledge graph conforming to the JSON schema. The 'id' of each ChunkNode MUST be the string form of the input 'chunk_id'. For example, chunk_id 0 MUST use ChunkNode id "0", never "chunk_0". Every relationship that refers to that chunk MUST use the same exact id.
            Your output MUST be a single JSON object containing a "results" key.
            The value of "results" should be a list, where each item corresponds to a processed chunk.

            **CRITICAL**: You MUST process every single chunk provided in the "LIST OF CHUNKS TO ANALYZE" and include an entry for it in your "results" list. 
            Each item in the list must contain the original 'chunk_id' and the extracted 'graph'.
            If you cannot extract a graph for a chunk, return its 'chunk_id' with an empty 'graph' object.

            EXAMPLE OUTPUT FORMAT:
            {{
              "results": [
                {{
                  "chunk_id": 0,
                  "graph": {{ "nodes": [...], "relationships": [...] }}
                }},
                {{
                  "chunk_id": 1,
                  "graph": {{ "nodes": [...], "relationships": [...] }}
                }}
              ]
            }}
            """

            # Construct the final prompt
            prompt = f"{instructional_prompt}\nLIST OF CHUNKS TO ANALYZE:\n{json.dumps(chunks_for_prompt, indent=2)}"

            # Define the expected output schema for the dispatcher
            graph_schema = ExtractedGraph.model_json_schema()
            graph_defs = graph_schema.pop("$defs", {})
            output_schema = {
                "$defs": graph_defs,
                "type": "object",
                "properties": {
                    "results": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "chunk_id": {"type": "integer"},
                                "graph": graph_schema,
                            },
                            "required": ["chunk_id", "graph"],
                        },
                    }
                },
                "required": ["results"],
            }

            llm_response_str = None
            try:
                # --- Re-implement the dispatcher logic using the new connector ---
                if provider in ["azure", "openai"]:
                    client = await IngestionLLMConnector.get_openai_client(ctx, user, db, provider)
                    # Build a Responses API request using structured output (json_schema)
                    # instead of the legacy chat.completions json_object mode.
                    # The schema is supplied via `text.format` so it does NOT need to be
                    # injected into the prompt text.
                    responses_req = LlmResponsesOpenaiConfig(
                        provider=provider,
                        model=model_name,
                        input=[{"role": "user", "content": prompt}],
                        text={
                            "format": {
                                "type": "json_schema",
                                "name": "extracted_graph",
                                "schema": output_schema,
                                "strict": False,
                            }
                        },
                        # No tools — pure one-shot JSON extraction. The default
                        # tools list includes code_interpreter/file_search/external
                        # functions, so we must explicitly set tools=None to strip them.
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
                    llm_response_str = response.output_text if response else None
                elif provider == "openrouter":
                    # OpenRouter uses the Chat Completions API (OpenAI-compatible).
                    # Structured output is via response_format json_schema (not Responses API).
                    openrouter_conn = await IngestionLLMConnector.get_openrouter_client(ctx, user, db)
                    chat_req = OpenRouterChatReq(
                        model=model_name,
                        messages=[{"role": "user", "content": prompt}],
                        json_schema=output_schema,
                        structured_output_name="extracted_graph",
                        structured_output_strict=False,
                        tools=[],
                        stream=False,
                    )
                    runner = OpenRouterChat(client=openrouter_conn.client)
                    chat_res = await runner.chat(
                        app_state=app_state,
                        chat_req=chat_req,
                        user=user,
                        ctx=ctx,
                        db=db,
                    )
                    llm_response_str = (
                        chat_res.choices[-1].message.content
                        if chat_res and chat_res.choices
                        else None
                    )
                elif provider == "gemini":
                    # Gemini native SDK with response_mime_type + response_json_schema.
                    gemini_client = await IngestionLLMConnector.get_gemini_client(ctx, user, db)
                    gemini_schema = fix_const_fields(output_schema)
                    gemini_schema = strip_unsupported_keys(
                        gemini_schema,
                        keys_to_remove={"default", "examples", "control_type", "placeholder"},
                    )
                    gen_config = gemini_types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_json_schema=gemini_schema,
                        temperature=0.0,
                    )
                    response = await gemini_client.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=gen_config,
                    )
                    TokenUsageService.record(
                        ctx=ctx,
                        surface=LlmSurface.gemini,
                        app_state=app_state,
                        usage=getattr(response, "usage_metadata", None),
                        model=model_name,
                        loop=1,
                    )
                    # Extract text from the first candidate's content parts.
                    parts = (
                        response.candidates[0].content.parts
                        if response and response.candidates
                        else None
                    )
                    llm_response_str = GeminiMessageNormalizer.extract_text_from_parts(parts) if parts else None
                elif provider == "anthropic":
                    # Claude has no native JSON mode; inject schema into prompt + system instruction.
                    claude_client = await IngestionLLMConnector.get_claude_client(ctx, user, db)
                    chat_req = ChatReqClaude(
                        model=model_name,
                        messages=[
                            {"role": "user", "content": f"{prompt}\n\nJSON SCHEMA: {json.dumps(output_schema)}"}
                        ],
                        system="You are a graph extraction assistant. Respond ONLY with valid JSON matching the provided schema. Do not include any prose, markdown, or code fences.",
                        tools=[],
                        stream=False,
                    )
                    chat_res = await claude_client.claude_chat.chat(
                        user=user,
                        ctx=ctx,
                        app_state=app_state,
                        chat_req=chat_req,
                        db=db,
                    )
                    # Concatenate text from TextBlock / BetaTextBlock content blocks.
                    if chat_res and chat_res.content:
                        llm_response_str = "".join(
                            block.text
                            for block in chat_res.content
                            if isinstance(block, (TextBlock, BetaTextBlock)) and block.text
                        )
                    else:
                        llm_response_str = None
                else:
                    raise NotImplementedError(
                        f"Provider '{provider}' not implemented in GraphRAGProcessor."
                    )

                # DEBUG LOG #4: Log the raw response string from the dispatcher immediately.
                logger.bind(llm_response=llm_response_str).trace(
                    "Raw LLM response string received from dispatcher."
                )

                if not llm_response_str:
                    raise GraphValidationException("LLM returned an empty response.")

                try:
                    parsed_response = json.loads(llm_response_str)
                    _validate_graph_response_shape(parsed_response, batch)
                    batch_map = _normalize_graph_response(parsed_response, batch)

                    # --- VALIDATION STAGE 1: Logical Consistency ---
                    _validate_graph_consistency(parsed_response, batch)

                    # --- VALIDATION STAGE 2: Pydantic Schema Conformance ---
                    graphs_from_response = []

                    for result in parsed_response.get("results", []):
                        if "graph" in result and result["graph"]:
                            # This will raise pydantic.ValidationError if the schema is wrong
                            graph = ExtractedGraph.model_validate(result["graph"])
                            # CRITICAL FIX: Ignore the LLM's 'order' and re-assign the correct one.
                            local_chunk_id = str(result.get("chunk_id", -1))
                            if local_chunk_id in batch_map:
                                for node in graph.nodes:
                                    if isinstance(node, ChunkNode):
                                        node.order = int(local_chunk_id)
                            graphs_from_response.append(graph)
                except (
                    json.decoder.JSONDecodeError,
                    ValidationError,
                    GraphValidationException,
                ) as validation_error:
                    logger.bind(llm_response=llm_response_str).warning(
                        f"LLM response validation failed: {type(validation_error).__name__} - {validation_error}. Triggering retry."
                    )
                    # Re-raise the specific exception to be caught by the @retry decorator
                    raise validation_error

                # Record successful attempt
                token_analyzer.record_batch_usage(
                    batch, instructional_prompt, llm_response_str, "SUCCESS"
                )

                return graphs_from_response
            except Exception as e:
                # Record failed attempt
                error_msg = f"{type(e).__name__}: {str(e)}"
                token_analyzer.record_batch_usage(
                    batch, instructional_prompt, llm_response_str, "FAILURE", error_msg
                )
                # Log the specific error and re-raise it to trigger the tenacity retry mechanism.
                logger.error(f"Failed to process a batch for doc '{document_id}'. Error: {e}")
                raise

    def _merge_graphs(self, main_graph: ExtractedGraph, partial_graph: ExtractedGraph):
        # A more advanced version could merge nodes with the same ID, but MERGE in Cypher handles this.
        main_graph.nodes.extend(partial_graph.nodes)
        main_graph.relationships.extend(partial_graph.relationships)
        main_graph.aliases.extend(partial_graph.aliases)
        main_graph.party_groups.extend(partial_graph.party_groups)

    def _normalize_data(self, graph: ExtractedGraph):
        """Converts string values for dates, numbers, etc., into their proper Python types."""
        for node in graph.nodes:
            if hasattr(node, "properties"):
                for key, value in node.properties.items():
                    if "date" in key.lower() and isinstance(value, str):
                        try:
                            # Handle ISO format with or without 'Z'
                            clean_value = value.replace("Z", "+00:00")
                            node.properties[key] = datetime.fromisoformat(clean_value)
                        except (ValueError, TypeError):
                            pass  # Keep as string if parsing fails
                    elif any(k in key.lower() for k in ["amount", "value", "rate"]) and isinstance(
                        value, str
                    ):
                        try:
                            # Remove currency symbols, commas, etc.
                            node.properties[key] = float(re.sub(r"[^\d.]", "", value))
                        except (ValueError, TypeError):
                            pass
