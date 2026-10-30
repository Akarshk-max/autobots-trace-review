# src/autobots/action/action_type/action_multimodal/action_multimodal_graph_rag_query.py
# Second version of the GraphRAGQuery action as a thin client
import asyncio
import re
from typing import List, Literal, Optional, Type

import httpx
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field, HttpUrl

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks, Metadata
from src.autobots.action.action_type.abc.ActionABC import ActionABC
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.ingestion.job_status import JobState
from src.autobots.ingestion.polling import poll_for_job_completion
from src.autobots.knowledge_base.kb_doc_model import KnowledgeBaseRole
from src.autobots.knowledge_base.kb_service import (
    filter_kbs_by_role,
    list_accessible_kbs_for_user,
    rank_kbs_by_ref,
    resolve_kb_reference_for_user,
)
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState


class GraphRAGQueryConfig(BaseModel):
    query: Optional[str] = Field(
        default=None,
        description="A default question to ask the knowledge base. Can be overridden in chat. Can be left blank if only ingesting files.",
        json_schema_extra={"control_type": "text_input", "title": "Query (Optional)"},
    )
    urls: Optional[List[HttpUrl]] = Field(
        default=None,
        description="A list of file URLs to ingest into the knowledge base before performing a query.",
        json_schema_extra={
            "control_type": "text_input",
            "title": "File URLs to Ingest (Optional)",
            "placeholder": '["https://example.com/file1.pdf", "https://example.com/file2.docx"]',
        },
    )
    collection_name: str = Field(
        default="synced_files",
        description="The Weaviate collection to use for initial semantic search.",
        json_schema_extra={"control_type": "text_input"},
    )
    vector_search_top_k: int = Field(
        default=20,
        description="Number of initial candidate chunks to retrieve from Weaviate.",
        json_schema_extra={"control_type": "text_input"},
    )
    graph_traversal_depth: int = Field(
        default=1,
        ge=1,
        le=5,
        description="How many hops to explore in the knowledge graph from the initial chunks (1-5).",
        json_schema_extra={"control_type": "text_input"},
    )
    docs_context_top_k: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Number of full source documents to include for richer context (0-10). More documents may improve answer quality but increase processing time.",
        json_schema_extra={"control_type": "text_input"},
    )
    kb_id: Optional[str] = Field(
        default=None,
        description="Optional knowledge base ID. If provided, search/ingest is scoped to this KB.",
        json_schema_extra={"control_type": "text_input", "title": "Knowledge Base ID (Optional)"},
    )
    kb_name: Optional[str] = Field(
        default=None,
        description=(
            "Optional human-facing knowledge base name. "
            "If kb_id is not provided but kb_name is, the backend will resolve the name to an ID."
        ),
        json_schema_extra={"control_type": "text_input", "title": "Knowledge Base Name (Optional)"},
    )
    scope: Optional[Literal["personal", "kb", "all"]] = Field(
        default=None,
        description="Explicit search scope: 'personal' (user's private KB), 'kb' (specific shared KB), or 'all' (all accessible KBs).",
        json_schema_extra={"control_type": "text_input", "title": "Search Scope (Optional)"},
    )


class ActionGraphRAGQuery(
    ActionABC[GraphRAGQueryConfig, GraphRAGQueryConfig, GraphRAGQueryConfig, DataBlock, DataBlocks]
):
    type = ActionType.multimodal_graph_rag_query

    # kb_<12 hex> (matches current kb_id generator kb_{uuid.hex[:12]})
    _KB_ID_RE = re.compile(r"^kb_[0-9a-f]{12}$", re.IGNORECASE)

    # --- FEATURE TOGGLE ---
    # Set to True to enable the original, combined ingest-then-query behavior.
    # Set to False to use the safer behavior of separating ingest and query actions.
    _ACTIVATE_INGEST_THEN_QUERY = False

    @staticmethod
    def get_description() -> str:
        return (
            "GraphRAGQuery: Thin-client wrapper that interacts with a remote RAG "
            "service to ingest files into a knowledge base and/or run graph-augmented "
            "retrieval-augmented-generation (RAG) queries. The action supports three "
            "primary intents depending on the input: (1) ingest files, (2) run a "
            "query against the permanent knowledge base, or (3) submit files for "
            "ingestion and then ask a question (the ingest-then-query flow is feature-"
            "gated).\n\n"
            "Input (config):\n"
            "  {\n"
            '      "query": "optional default question",        # optional\n'
            '      "urls": ["https://.../file1.pdf", ...],      # optional list of S3/file URLs\n'
            '      "kb_id": "kb_123...",                        # optional: specific KB ID to ingest/search\n'
            '      "kb_name": "Finance Docs",                   # optional: KB name (resolved to ID if unique)\n'
            '      "scope": "personal" | "kb" | "all",          # optional: SEARCH scope (see notes below)\n'
            '      "collection_name": "synced_files",           # which collection to search\n'
            '      "vector_search_top_k": 20,                   # number of vector candidates\n'
            '      "graph_traversal_depth": 1,                  # graph hops (1-5)\n'
            '      "docs_context_top_k": 3                      # how many full docs to include\n'
            "  }\n\n"
            "Behavior / semantics:\n"
            "  - If only `urls` are provided → queues each URL for ingestion and returns\n"
            "    an immediate acknowledgement containing queued job counts. Ingestion is\n"
            "    performed by the external RAG service.\n\n"
            "  - If only a `query` is provided → performs a search against the permanent\n"
            "    knowledge base via the RAG service and returns the retrieved context or\n"
            "    answer. The action will retry search requests with exponential backoff to\n"
            "    tolerate brief indexing delays.\n\n"
            "  - If both `urls` and `query` are provided → two possible flows:\n"
            "      * Default (safe) behavior: the action queues ingestion jobs and returns\n"
            "        an instructional message asking the caller to re-run the query later\n"
            "        once ingestion completes.\n"
            "      * If ingest-then-query is explicitly enabled (feature toggle), the\n"
            "        action will queue ingestion jobs, poll for completion (with timeouts\n"
            "        and error handling), and then perform the search once ingestion is\n"
            "        available. This combined flow may wait for the graph backend and can\n"
            "        surface errors if ingestion or graph construction fails.\n\n"
            "Knowledge base scoping:\n"
            "  - Ingestion always targets exactly ONE destination:\n"
            "      * personal KB (default) when kb_id/kb_name is not provided, or\n"
            "      * a specific shared KB when kb_id (or resolvable kb_name) is provided.\n"
            "  - `scope` is a SEARCH-only concept:\n"
            '      * "personal": search only the user\'s personal KB\n'
            '      * "kb": search only the specified KB (requires kb_id or kb_name)\n'
            '      * "all": search across all KBs the user can access\n'
            '    NOTE: "all" is NOT valid for ingestion.\n\n'
            "Requirements & notes:\n"
            "  - The user must supply a valid access token (stored on the user object)\n"
            "    used to authenticate requests to the external RAG service.\n"
            "  - This action is a thin client: ingestion and search work are performed by\n"
            "    an external RAG service endpoint; the action composes HTTP requests and\n"
            "    interprets responses.\n\n"
            "Output:\n"
            "  A DataBlocks object containing one or more DataBlock entries. Typical\n"
            "  responses include: acknowledgement text for queued ingestion jobs, a\n"
            "  structured search result set returned by the RAG service, or an\n"
            "  instructional message explaining next steps. On error, an exception is\n"
            "  returned."
        )

    @staticmethod
    def get_config_create_type() -> Type[GraphRAGQueryConfig]:
        return GraphRAGQueryConfig

    @staticmethod
    def get_config_update_type() -> Type[GraphRAGQueryConfig]:
        return GraphRAGQueryConfig

    @staticmethod
    def get_config_type() -> Type[GraphRAGQueryConfig]:
        return GraphRAGQueryConfig

    @staticmethod
    def get_input_type() -> Type[DataBlock]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[DataBlocks]:
        return DataBlocks

    def __init__(
        self,
        action_config: GraphRAGQueryConfig,
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase | None = None,
    ):
        super().__init__(action_config=action_config, user=user, db=db)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        """
        This action is a thin client. It forwards the query to the standalone RAG service.
        """
        settings = SettingsProvider.sget()

        # For run_action, the query comes from the input DataBlock.
        # For run_tool, it comes from the config. We prioritize the input.
        query = action_input.text or self.action_config.query

        has_files = action_input.urls and len(action_input.urls) > 0
        has_query = query and query.strip()

        # Local effective scope (do NOT mutate config)
        effective_scope = getattr(self.action_config, "scope", None)

        # --- Resolve knowledge base context once per run into a local variable. ---
        # Prefer explicit kb_id. If not set, attempt kb_name resolution with fuzzy suggestions.
        resolved_kb_id = getattr(self.action_config, "kb_id", None)
        kb_name = getattr(self.action_config, "kb_name", None)

        # Decide required role for candidate filtering (ingestion needs WRITE, search needs READ)
        required_role = KnowledgeBaseRole.READ_WRITE if has_files else KnowledgeBaseRole.READ

        if not resolved_kb_id and kb_name and self.db is not None and self.user is not None:
            kb_name_str = str(kb_name).strip()

            # If user/LLM put an ID into kb_name, accept it as kb_id
            # IMPORTANT: Only accept real kb_id format (avoid treating KB names like "kb_finance" as IDs)
            if self._KB_ID_RE.match(kb_name_str):
                resolved_kb_id = kb_name_str.lower()
            else:
                try:
                    res = await resolve_kb_reference_for_user(
                        db=self.db,
                        user=self.user,
                        kb_ref=kb_name_str,
                        required_role=required_role,
                        ctx=ctx,
                    )

                    if res.kind == "resolved":
                        resolved_kb_id = res.kb_id

                    elif res.kind == "personal":
                        # If there is a shared KB with a colliding name, ask the user which one.
                        if res.candidates:
                            lines = "\n".join(
                                [
                                    f"- Shared KB '{c.name}' (kb_id={c.kb_id}, role={c.role.value})"
                                    for c in res.candidates
                                ]
                            )
                            text = (
                                f"Your input '{kb_name_str}' looks like it refers to your personal KB (private).\n"
                                f"However, you also have access to shared KB(s) with a similar name:\n"
                                f"{lines}\n\n"
                                "Reply with one of:\n"
                                "- 'personal' to use your private KB, OR\n"
                                "- kb_id=... to use a shared KB.\n"
                            )
                            return DataBlocks(blocks=[DataBlock(text=text, metadata=Metadata())])

                        # No collision: interpret as personal intent.
                        # For ingestion: personal is the default (omit kb_id).
                        # For search: must set scope='personal' to avoid worker defaulting to scope='all'.
                        # Force personal semantics for BOTH ingestion and search.
                        # This prevents mis-set tool config like scope='kb' or scope='all' from causing
                        # validation failures when kb_name indicates personal intent.
                        if effective_scope and effective_scope != "personal":
                            logger.bind(ctx=ctx).warning(
                                f"Overriding conflicting scope '{effective_scope}' to 'personal' "
                                f"due to kb_name='{kb_name_str}'."
                            )
                        effective_scope = "personal"

                    elif res.kind == "all":
                        # "all" is search-only
                        if has_files:
                            return ValueError(
                                "You requested 'all' knowledge bases, but ingestion cannot target scope='all'. "
                                "Please omit kb_name/scope to ingest into your personal KB, or provide kb_id/kb_name for a specific shared KB."
                            )
                        # If user explicitly set a conflicting scope, return a clear error
                        if effective_scope and effective_scope != "all":
                            return ValueError(
                                "Conflicting scope: kb_name indicates searching across ALL KBs, "
                                f"but scope is set to '{effective_scope}'. Remove scope or set scope='all'."
                            )
                        if has_query:
                            effective_scope = "all"

                    elif res.kind == "ambiguous":
                        lines = "\n".join(
                            [
                                f"- '{c.name}' (kb_id={c.kb_id}, role={c.role.value})"
                                for c in res.candidates
                            ]
                        )
                        text = (
                            f"Multiple knowledge bases match the name '{kb_name_str}'.\n\n"
                            f"Please specify which one by KB ID:\n{lines}\n"
                        )
                        return DataBlocks(blocks=[DataBlock(text=text, metadata=Metadata())])

                    elif res.kind == "forbidden":
                        # Exact match exists but user lacks required role (especially for ingestion)
                        lines = "\n".join(
                            [
                                f"- '{c.name}' (kb_id={c.kb_id}, your_role={c.role.value})"
                                for c in res.candidates
                            ]
                        )
                        if has_files:
                            text = (
                                f"You have access to a knowledge base named '{kb_name_str}', but you don't have write permission to ingest into it.\n"
                                f"Matching KB(s):\n{lines}\n\n"
                                "Options:\n"
                                "- Ingest into your personal KB (omit kb_name/kb_id), OR\n"
                                "- Ask a KB admin to grant you read_write access.\n"
                            )
                        else:
                            text = (
                                f"You have access to KB(s) matching '{kb_name_str}', but you do not have permission to use them for this operation.\n"
                                f"{lines}\n"
                            )
                        return DataBlocks(blocks=[DataBlock(text=text, metadata=Metadata())])

                    else:  # not_found
                        if res.candidates:
                            lines = "\n".join(
                                [
                                    f"- '{c.name}' (kb_id={c.kb_id}, role={c.role.value})"
                                    for c in res.candidates
                                ]
                            )
                            base_text = (
                                f"I couldn't find a knowledge base named '{kb_name_str}'. Closest matches:\n"
                                f"{lines}\n\n"
                            )
                        else:
                            base_text = (
                                f"I couldn't find a knowledge base named '{kb_name_str}', and no close matches were found.\n\n"
                            )

                        # If we don't have db/user, we cannot list KBs; return best-effort guidance.
                        if self.db is None or self.user is None:
                            text = base_text + "Tip: omit kb_name/kb_id to use your personal KB.\n"
                            return DataBlocks(blocks=[DataBlock(text=text, metadata=Metadata())])

                        accessible = await list_accessible_kbs_for_user(db=self.db, user=self.user)

                        # Policy:
                        # - If number of KBs is limited => show all
                        # - If too many => show top 20 ranked
                        MAX_SHOW_ALL = 20
                        MAX_SHOW_TOP = 20

                        if has_files:
                            # Ingestion: show only KBs where user can WRITE (avoid suggesting read-only)
                            writable = filter_kbs_by_role(accessible, required_role)
                            if not writable:
                                text = (
                                    base_text
                                    + "You don't have write access to any shared knowledge bases.\n"
                                    + "Tip: omit kb_name/kb_id to ingest into your personal KB, or ask an admin for read_write access.\n"
                                )
                                return DataBlocks(blocks=[DataBlock(text=text, metadata=Metadata())])

                            if len(writable) <= MAX_SHOW_ALL:
                                lines2 = "\n".join(
                                    [f"- '{c.name}' (kb_id={c.kb_id}, role={c.role.value})" for c in writable]
                                )
                                text = (
                                    base_text
                                    + "Shared knowledge bases you can ingest into:\n"
                                    + f"{lines2}\n\n"
                                    + "Reply with the exact KB name or the KB ID (e.g., kb_abc123def456). "
                                      "Or say 'personal' to use your private KB.\n"
                                )
                                return DataBlocks(blocks=[DataBlock(text=text, metadata=Metadata())])

                            ranked = rank_kbs_by_ref(kb_name_str, writable)[:MAX_SHOW_TOP]
                            lines2 = "\n".join(
                                [f"- '{kb.name}' (kb_id={kb.kb_id}, role={kb.role.value})" for _, kb in ranked]
                            )
                            text = (
                                base_text
                                + f"Showing top {MAX_SHOW_TOP} ranked writable KBs (out of {len(writable)}):\n"
                                + f"{lines2}\n\n"
                                + "Reply with the exact KB name or the KB ID (e.g., kb_abc123def456). "
                                  "Or say 'personal' to use your private KB.\n"
                            )
                            return DataBlocks(blocks=[DataBlock(text=text, metadata=Metadata())])

                        # Search (non-ingestion): show all accessible if limited; else top 20 ranked.
                        if not accessible:
                            text = base_text + "Tip: omit kb_name/kb_id to use your personal KB.\n"
                            return DataBlocks(blocks=[DataBlock(text=text, metadata=Metadata())])

                        if len(accessible) <= MAX_SHOW_ALL:
                            lines2 = "\n".join(
                                [f"- '{c.name}' (kb_id={c.kb_id}, role={c.role.value})" for c in accessible]
                            )
                            text = (
                                base_text
                                + "Shared knowledge bases you can access:\n"
                                + f"{lines2}\n\n"
                                + "Reply with kb_id=... or the exact KB name.\n"
                            )
                            return DataBlocks(blocks=[DataBlock(text=text, metadata=Metadata())])

                        ranked = rank_kbs_by_ref(kb_name_str, accessible)[:MAX_SHOW_TOP]
                        lines2 = "\n".join(
                            [f"- '{kb.name}' (kb_id={kb.kb_id}, role={kb.role.value})" for _, kb in ranked]
                        )
                        text = (
                            base_text
                            + f"Showing top {MAX_SHOW_TOP} ranked KBs (out of {len(accessible)}):\n"
                            + f"{lines2}\n\n"
                            + "Reply with kb_id=... or the exact KB name.\n"
                        )
                        return DataBlocks(blocks=[DataBlock(text=text, metadata=Metadata())])

                except Exception as e:
                    logger.bind(ctx=ctx).exception(
                        f"Unexpected error while resolving knowledge base reference '{kb_name_str}': {e}"
                    )
                    return e

        # --- Validate scope semantics ---
        # Ingestion can only go to personal KB (default) or a specific KB (kb_id/kb_name).
        if has_files and effective_scope == "all":
            return ValueError(
                "scope='all' is only supported for SEARCH. "
                "When ingesting files, omit scope (personal KB) or use scope='kb' with kb_id/kb_name."
            )
        if has_files and effective_scope == "kb" and not resolved_kb_id:
            return ValueError(
                "scope='kb' requires kb_id or kb_name (which must resolve to a unique KB)."
            )
        if has_files and effective_scope == "personal" and resolved_kb_id:
            return ValueError(
                "scope='personal' cannot be used together with kb_id/kb_name. "
                "Remove kb_id/kb_name to ingest into the personal KB, or set scope='kb' to ingest into a shared KB."
            )
        # Search: if user explicitly requested kb-scope but no kb_id could be resolved, fail early.
        if (not has_files) and has_query and effective_scope == "kb" and not resolved_kb_id:
            return ValueError("scope='kb' requires kb_id or kb_name (which must resolve to a unique KB).")
        if (not has_files) and has_query and effective_scope == "personal" and resolved_kb_id:
            return ValueError("scope='personal' cannot be used together with kb_id/kb_name.")
        if (not has_files) and has_query and effective_scope == "all" and resolved_kb_id:
            return ValueError(
                "scope='all' cannot be combined with kb_id/kb_name. "
                "Remove kb_id/kb_name to search all KBs, or set scope='kb' to search one KB."
            )
        if not has_files and not has_query:
            return ValueError(
                "Please provide a file to add to your knowledge base, a question to ask, or both."
            )

        if not self.user.access_token:
            return ValueError("An access token is required to communicate with the RAG service.")

        headers = {"Authorization": f"Bearer {self.user.access_token}"}
        rag_service_base_url = settings.INGESTION_WORKER_URL

        # --- INTENT 3: Ingest, wait for completion, and then query (toggle is OFF) ---
        if has_files and has_query and not self._ACTIVATE_INGEST_THEN_QUERY:
            # NEW BEHAVIOR (Default): Ingest files and guide the user to ask again.
            logger.info("Received combined Ingest and Query request. Separating into two steps.")

            # --- Step 1: Perform the ingestion part ---
            job_ids = []
            failed_urls = []
            async with httpx.AsyncClient(timeout=60.0) as client:
                for s3_url in action_input.urls:
                    try:
                        parsed_url = HttpUrl(s3_url)
                        original_filename = (
                            parsed_url.path.split("/")[-1] if parsed_url.path else "file_from_s3"
                        )
                        ingest_payload = {
                            "s3_url": str(s3_url),
                            "original_filename": original_filename,
                            "collection_name": self.action_config.collection_name,
                            "targets": ["weaviate", "graphrag"],
                        }
                        if resolved_kb_id:
                            ingest_payload["kb_id"] = resolved_kb_id

                        ingest_endpoint = f"{rag_service_base_url}/api{settings.API_V1_INGESTION}/knowledge-base/ingest-from-s3-url"
                        ingest_response = await client.post(
                            ingest_endpoint, json=ingest_payload, headers=headers
                        )
                        ingest_response.raise_for_status()
                        job_ids.append(ingest_response.json()["job_id"])
                    except Exception as e:
                        logger.exception(f"Failed to queue ingestion for {s3_url}: {e}")
                        failed_urls.append(str(s3_url))

            ingest_status_message = ""
            if job_ids:
                ingest_status_message += (
                    f"Successfully queued {len(job_ids)} file(s) for ingestion."
                )
            if failed_urls:
                ingest_status_message += f" Failed to queue {len(failed_urls)} file(s)."

            # --- Step 2: Return instructional message ---
            instructional_message = (
                "\nThe file(s) are being added to your knowledge base. This may take a few minutes. "
                "Please ask your question again in a new message."
            )

            final_text = ingest_status_message + instructional_message
            return DataBlocks(blocks=[DataBlock(text=final_text, metadata=Metadata())])

        # --- INTENT 3: Ingest, wait for completion, and then query (toggle is ON) ---
        elif has_files and has_query and self._ACTIVATE_INGEST_THEN_QUERY:
            # OLD BEHAVIOR: Attempt the combined ingest-then-query flow.
            logger.info(
                f"Received Ingest-and-Query request. Submitting {len(action_input.urls)} file(s) for ingestion..."
            )
            job_ids = []
            async with httpx.AsyncClient(timeout=60.0) as client:
                for s3_url in action_input.urls:
                    try:
                        parsed_url = HttpUrl(s3_url)
                        original_filename = (
                            parsed_url.path.split("/")[-1] if parsed_url.path else "file_from_s3"
                        )

                        ingest_payload = {
                            "s3_url": str(s3_url),
                            "original_filename": original_filename,
                            "collection_name": self.action_config.collection_name,
                            "targets": ["weaviate", "graphrag"],
                        }
                        if resolved_kb_id:
                            ingest_payload["kb_id"] = resolved_kb_id

                        # <<< DEBUG LOG: Verify payload before sending >>>
                        logger.bind(payload=ingest_payload).debug(
                            "Sending ingest-and-query payload to API"
                        )

                        ingest_endpoint = f"{rag_service_base_url}/api{settings.API_V1_INGESTION}/knowledge-base/ingest-from-s3-url"
                        ingest_response = await client.post(
                            ingest_endpoint, json=ingest_payload, headers=headers
                        )
                        ingest_response.raise_for_status()
                        response_data = ingest_response.json()
                        job_ids.append(response_data["job_id"])
                    except Exception as e:
                        logger.exception(f"Failed to queue ingestion for {s3_url}: {e}")
                        return Exception(f"Failed to queue ingestion for {s3_url}: {e}")

            # --- ROBUST POLLING (SIMPLIFIED) ---
            # The worker now performs its own inline probe and sets a definitive final state.
            # The client only needs to poll for one of these terminal states.
            valid_job_ids = [jid for jid in job_ids if jid]
            if valid_job_ids:
                # This workflow requires the graph, so we must wait for full availability.
                # We use the enhanced poller to wait until 'graphrag_status' is terminal.
                polling_tasks = [
                    poll_for_job_completion(
                        job_id, db=self.db, timeout_seconds=240, required_backends=["graphrag"]
                    )
                    for job_id in valid_job_ids
                ]

                job_results = await asyncio.gather(*polling_tasks, return_exceptions=True)

                for i, result in enumerate(job_results):
                    job_id = valid_job_ids[i]
                    if isinstance(result, Exception):
                        raise Exception(f"Polling for job {job_id} failed.") from result
                    # The overall status can be PARTIAL_AVAILABLE if graph fails, which is a valid terminal state.
                    if result["status"] == JobState.FAILED.value:
                        raise Exception(
                            f"Ingestion job {job_id} failed with status 'FAILED': {result.get('error_message', 'No error details provided.')}"
                        )
                    if result.get("graphrag_status") == "FAILED":
                        logger.warning(
                            f"GraphRAG ingestion for job {job_id} failed. Search will proceed with vector-only context."
                        )

        # --- Intent 1: "Just Ingest" (no query present) ---
        elif has_files:
            # This branch remains a PURE THIN CLIENT, making an HTTP call.
            # This is the correct pattern for fire-and-forget tasks.
            logger.info(f"Queueing {len(action_input.urls)} S3 URL(s) for ingestion...")
            job_ids = []
            failed_urls = []
            async with httpx.AsyncClient(timeout=60.0) as client:
                for s3_url in action_input.urls:
                    try:
                        parsed_url = HttpUrl(s3_url)
                        original_filename = (
                            parsed_url.path.split("/")[-1] if parsed_url.path else "file_from_s3"
                        )

                        ingest_payload = {
                            "s3_url": str(s3_url),
                            "original_filename": original_filename,
                            "collection_name": self.action_config.collection_name,
                            "targets": ["weaviate", "graphrag"],
                        }
                        if resolved_kb_id:
                            ingest_payload["kb_id"] = resolved_kb_id

                        # <<< DEBUG LOG: Verify payload before sending >>>
                        logger.bind(payload=ingest_payload).debug(
                            "Sending ingest-only payload to API"
                        )

                        ingest_endpoint = f"{rag_service_base_url}/api{settings.API_V1_INGESTION}/knowledge-base/ingest-from-s3-url"
                        ingest_response = await client.post(
                            ingest_endpoint, json=ingest_payload, headers=headers
                        )
                        ingest_response.raise_for_status()
                        response_data = ingest_response.json()
                        job_ids.append(response_data["job_id"])
                    except Exception as e:
                        logger.exception(f"Failed to queue ingestion for {s3_url}: {e}")
                        failed_urls.append(str(s3_url))

            response_text = ""
            if job_ids:
                response_text += f"Successfully queued {len(job_ids)} file(s) for ingestion."
            if failed_urls:
                response_text += f" Failed to queue {len(failed_urls)} file(s)."

            # Return immediately after queuing.
            return DataBlocks(blocks=[DataBlock(text=response_text, metadata=Metadata())])

        # --- INTENT 2: Just Query ---
        # FIX: Use Version 1's correct control flow - this executes for both query-only AND after ingest-and-query
        if has_query:
            logger.info("Performing a search on the permanent knowledge base...")
            search_payload = {
                "query": query,
                "collection_name": self.action_config.collection_name,
                "vector_search_top_k": self.action_config.vector_search_top_k,
                "graph_traversal_depth": self.action_config.graph_traversal_depth,
                "docs_context_top_k": self.action_config.docs_context_top_k,
            }
            # If we resolved a specific KB for this run, scope the search to it.
            if resolved_kb_id:
                search_payload["kb_id"] = resolved_kb_id
            if effective_scope:
                search_payload["scope"] = effective_scope

            try:
                # This loop handles the race condition where data is ingested but not yet vector-indexed.
                # We always allow a few retries, as a query might be made immediately after
                # a separate ingestion job has finished, creating the same race condition.
                max_attempts = 6
                base_backoff = 3  # Start with a 3-second wait for consistency

                search_endpoint = (
                    f"{rag_service_base_url}/api{settings.API_V1_INGESTION}/knowledge-base/search"
                )
                async with httpx.AsyncClient(timeout=120.0) as client:
                    for attempt in range(max_attempts):
                        logger.info(
                            f"Performing knowledge base search (Attempt {attempt + 1}/{max_attempts})..."
                        )
                        search_response = await client.post(
                            search_endpoint, json=search_payload, headers=headers
                        )
                        search_response.raise_for_status()
                        blocks = DataBlocks(**search_response.json())

                        # Check if the search returned a meaningful result or the default "not found" message.
                        if blocks.blocks and not (
                            "couldn't find any directly relevant information"
                            in blocks.blocks[0].text.lower()
                            or "internal error occurred" in blocks.blocks[0].text.lower()
                        ):
                            return blocks  # Success, return immediately
                        if attempt < max_attempts - 1:
                            wait_time = base_backoff * (
                                2**attempt
                            )  # Exponential backoff: 3s, 6s, 12s...
                            logger.warning(
                                f"Search returned no results on attempt {attempt + 1}. Retrying in {wait_time}s..."
                            )
                            await asyncio.sleep(wait_time)
                    return blocks  # Return the result of the last attempt
            except httpx.HTTPStatusError as e:
                logger.exception(
                    f"RAG service search returned an error: {e.response.status_code} - {e.response.text}"
                )
                error_detail = e.response.json().get(
                    "detail", "Unknown error from RAG service search"
                )
                return Exception(f"Error from RAG service search: {error_detail}")
            except Exception as e:
                logger.exception(
                    f"An unexpected error occurred while calling the RAG service search: {e}"
                )
                return e

    async def run_tool(
        self, action_config: GraphRAGQueryConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        """
        This is the tool entry point for the GraphRAG action, called by the ToolFactory.
        """
        try:
            # The ToolFactory has already instantiated the class with the user and db.
            # We just need to execute the action.

            # --- ADAPTER LOGIC ---
            # Convert the data from GraphRAGQueryConfig format to the required DataBlock format.
            # 1. Convert the list of HttpUrl objects to a list of plain strings.
            # 2. Handle the case where urls might be None by defaulting to an empty list.
            # 3. Handle the case where the query might be None by defaulting to an empty string.
            string_urls = [str(url) for url in action_config.urls] if action_config.urls else []
            query_text = action_config.query or ""

            action_input = DataBlock(text=query_text, urls=string_urls)

            return await self.run_action(ctx=ctx, app_state=app_state, action_input=action_input)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in run_tool for GraphRAGQuery: {e}")
            return e
