import asyncio
import inspect
import io
import json
import mimetypes
from tempfile import SpooledTemporaryFile
from typing import Any, TypedDict

from anthropic import AsyncAnthropic, AsyncStream
from anthropic.types import (
    Message,
    MessageStreamEvent,
    RedactedThinkingBlock,
    ServerToolUseBlock,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    WebSearchToolResultBlock,
)
from anthropic.types.beta import (
    BetaBashCodeExecutionToolResultBlock,
    BetaCodeExecutionToolResultBlock,
    BetaMCPToolResultBlock,
    BetaMCPToolUseBlock,
    BetaMemoryTool20250818Command,
    BetaRedactedThinkingBlock,
    BetaServerToolUseBlock,
    BetaTextBlock,
    BetaTextEditorCodeExecutionToolResultBlock,
    BetaThinkingBlock,
    BetaToolUseBlock,
    BetaWebFetchToolResultBlock,
    BetaWebSearchToolResultBlock,
)
from fastapi import HTTPException, UploadFile
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from starlette.datastructures import Headers
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

from src.autobots import SettingsProvider
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.conn.aws.aws_s3 import get_public_s3, get_s3
from src.autobots.conn.claude.chat_model import (
    ChatReqClaude,
    ClaudeMessageParam,
    ClaudeToolResultEnvelope,
)
from src.autobots.conn.claude.context_management import ClaudeContextManagementHelper
from src.autobots.conn.claude.context_policy import ClaudeContextPolicy, ClaudeContextPolicyInput
from src.autobots.conn.claude.file_pipeline import ClaudeFilePipeline
from src.autobots.conn.claude.model_registry import ClaudeModelRegistry
from src.autobots.conn.claude.prompt_caching import ClaudePromptCachingHelper
from src.autobots.conn.claude.token_counting import ClaudeTokenCountingHelper
from src.autobots.conn.openai.openai_responses.tool_args_streamer import ToolArgsStreamer
from src.autobots.core.cancellation.run_instructions import RunInstruction, drain_instructions
from src.autobots.core.logging.app_code import AppCode
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.llm.chat_snapshot.provider_snapshots import snapshot_claude
from src.autobots.llm.tools.action_chat_history_query_claude import (
    ActionChatHistoryQueryClaude,
    ChatHistoryQueryReq,
)
from src.autobots.llm.tools.action_tool_results_query_claude import (
    ActionToolResultsQueryClaude,
    ToolResultsQueryReq,
)
from src.autobots.llm.tools.tool_factory_claude import ToolFactoryClaude
from src.autobots.mcp.user_mcp import UserMCP
from src.autobots.server_sent_events.server_sent_events import Notify, NotifyPayload
from src.autobots.server_sent_events.topics import text_topic
from src.autobots.token_usage.token_usage_service import LlmSurface, TokenUsageService
from src.autobots.user.user_orm_model import UserORM

# Global variables
MAX_CLAUDE_CONTAINER_FILES = 16
SAFE_MAX_NEW_ATTACHMENTS_PER_CALL = 15

class ComputerUseConfigParam(TypedDict):
    enabled: bool

def _is_context_window_error_text(text: str) -> bool:
    lower = (text or "").lower()
    return any(
        phrase in lower
        for phrase in (
            "request size exceeds model context window",
            "model_context_window_exceeded",
            "context window exceeded",
            "prompt is too long",
            "too many tokens",
            "request too large",
        )
    )


def _is_retryable_claude_exception(exc: BaseException) -> bool:
    if isinstance(exc, asyncio.CancelledError):
        return False
    if isinstance(exc, HTTPException):
        return exc.status_code in {408, 409, 425, 429, 500, 502, 503, 504, 529}

    text = str(exc)
    if _is_context_window_error_text(text):
        return False

    lower = text.lower()
    if "internal server error" in lower:
        return True
    
    if "invalid_request_error" in lower:
        return False

    return True


class AnthropicChat:
    def __init__(self, anthropic_client: AsyncAnthropic):
        self.client = anthropic_client

    @retry(
        retry=retry_if_exception(_is_retryable_claude_exception),
        stop=stop_after_attempt(5),
        wait=wait_random_exponential(multiplier=5, min=1, max=60),
        reraise=True,
    )
    async def chat(
        self,
        user: UserORM,
        ctx: Context,
        app_state: AppState,
        chat_req: ChatReqClaude,
        db: AsyncIOMotorDatabase | None = None,
        notify: Notify | None = None,
        tool_output_text_to_file_char_limit: int | None = None,
    ) -> Message | None:
        try:
            logger.bind(ctx=ctx,code=AppCode.CLAUDE).trace("Starting Anthropic chat")
            base_payload = chat_req.model_dump(exclude_none=False)
            working_req = ChatReqClaude.model_validate(base_payload)
            res = await self.chat_loop(
                user=user,
                ctx=ctx,
                app_state=app_state,
                chat_req=working_req,
                db=db,
                notify=notify,
                tool_output_text_to_file_char_limit=tool_output_text_to_file_char_limit,
            )
            for field_name in ChatReqClaude.model_fields:
                setattr(chat_req, field_name, getattr(working_req, field_name))
            if hasattr(chat_req, "__pydantic_fields_set__") and hasattr(working_req, "__pydantic_fields_set__"):
                chat_req.__pydantic_fields_set__ = set(working_req.__pydantic_fields_set__)
            logger.bind(ctx=ctx, code=AppCode.CLAUDE).trace("Completed Anthropic chat")
            return res
        except Exception as exc:
            logger.bind(ctx=ctx, code=AppCode.CLAUDE).exception("Error in Claude chat: {}", exc)
            raise

    @staticmethod
    def _deliver_instructions(
        ctx: Context,
        chat_req: ChatReqClaude,
        instructions: list[RunInstruction],
        notify: Notify | None,
    ) -> None:
        for instruction in instructions:
            block = {"type": "text", "text": instruction.as_user_message()}
            last = chat_req.messages[-1] if chat_req.messages else None
            if isinstance(last, dict) and last.get("role") == "user":
                content = last.get("content")
                if isinstance(content, list):
                    content.append(block)
                else:
                    last["content"] = [{"type": "text", "text": str(content or "")}, block]
            else:
                chat_req.messages.append(ClaudeMessageParam(role="user", content=[block]))
            logger.bind(ctx=ctx, code=AppCode.CLAUDE).info(
                f"Run instruction applied: {instruction.text}"
            )
            notify(ctx, f"Instruction applied: {instruction.text}") if notify else None

    async def _instructions_at_exit(
        self, ctx: Context, app_state: AppState, chat_req: ChatReqClaude, notify: Notify | None
    ) -> bool:
        pending = await drain_instructions(app_state)
        if not pending:
            return False
        self._deliver_instructions(ctx, chat_req, pending, notify)
        return True

    @staticmethod
    def _merge_generated_files(
        chat_req: ChatReqClaude,
        new_files: list[dict[str, Any]],
    ) -> None:
        existing_items = getattr(chat_req, "generated_files", None)
        if existing_items is None:
            return

        existing_s3_urls: set[str] = set()
        for raw_item in existing_items:
            if hasattr(raw_item, "model_dump"):
                item = raw_item.model_dump(exclude_none=True)
            elif isinstance(raw_item, dict):
                item = raw_item
            else:
                continue

            s3_url = item.get("s3_url") or item.get("url")
            if s3_url:
                existing_s3_urls.add(str(s3_url))

        for item in new_files:
            s3_url = item.get("s3_url") or item.get("url")
            if not s3_url:
                continue
            if str(s3_url) in existing_s3_urls:
                continue

            existing_items.append(item)
            existing_s3_urls.add(str(s3_url))

        if len(existing_items) > 100:
            chat_req.generated_files = existing_items[-100:]

    @staticmethod
    def _render_generated_files_context(new_files: list[dict[str, Any]]) -> str:
        lines = ["Generated files uploaded to S3 and available for later reference:"]
        for item in new_files:
            file_name = item.get("file_name") or item.get("filename") or "unnamed_file"
            file_id = item.get("file_id")
            s3_url = item.get("s3_url") or item.get("url")
            if not s3_url:
                continue

            suffix = f" (file_id={file_id})" if file_id else ""
            lines.append(f"- {file_name}{suffix}: {s3_url}")
        return "\n".join(lines)
    

    @staticmethod
    def _truncate_tool_output_text(text: Any, max_chars: int = 20000) -> str | None:
        if text is None:
            return None

        if not isinstance(text, str):
            text = str(text)

        text = text.strip()
        if not text:
            return None

        if len(text) <= max_chars:
            return text

        return text[:max_chars]


    @staticmethod
    async def _maybe_spill_large_query_json_to_file(
        *,
        ctx: Context,
        user: UserORM,
        chat_req: ChatReqClaude | None,
        tool_name: str,
        payload: dict[str, Any],
        max_chars: int | None,
        notify: Notify | None = None,
    ) -> DataBlock:
        """Ensure query tool outputs do not flood the model context.

        If the JSON payload is larger than max_chars, upload the full payload to S3
        and return a small JSON response containing a file URL + summary + a tiny preview.
        """
        json_text = json.dumps(payload, indent=2, ensure_ascii=False)
        if not max_chars or len(json_text) <= max_chars:
            return DataBlock(text=json_text)
        
        try:
            notify(ctx, f"{tool_name} output too large, saving to file") if notify else None
            s3 = get_public_s3()
            user_files = UserFiles(user=user, s3=s3)
            filename = f"{gen_uuid()}.json"
            file_metas = await user_files.upload_binary(
                ctx=ctx,
                file_obj=io.BytesIO(json_text.encode("utf-8")),
                filename=filename,
            )
            file_meta = file_metas[0]
            file_url = str(file_meta.url)

            # Make spill artifacts discoverable for explicit attachment/indexing, but do not auto-attach.
            if chat_req is not None:
                existing = [str(x) for x in (getattr(chat_req, "container_candidate_urls", None) or [])]
                existing = [url for url in existing if url and url != file_url]
                merged = existing + [file_url]
                merged = AnthropicChat._dedupe_urls(merged)
                max_candidates = 30
                if len(merged) > max_candidates:
                    merged = merged[-max_candidates:]
                chat_req.container_candidate_urls = merged

            
            results_value = payload.get("results")
            preview_results: Any = None
            if isinstance(results_value, list) and results_value:
                preview_results = results_value[:1]
            elif results_value is not None and not isinstance(results_value, list):
                # Non-list results should be rare with per-item JMESPath semantics, but keep safe.
                preview_results = results_value
            
            preview_payload: dict[str, Any] = {
                "spilled": True,
                "tool": tool_name,
                "file_url": file_url,
                "max_chars": max_chars,
                "total_count": payload.get("total_count"),
                "returned_count": payload.get("returned_count"),
                "summary": payload.get("summary"),
                "results_preview": preview_results,
                "next_steps": (
                    "Narrow your query and/or paginate with offset. "
                    "Only a preview is shown here; full results are in file_url."
                ),
            }
            preview_json = json.dumps(preview_payload, indent=2, ensure_ascii=False)
            if len(preview_json) > max_chars:
                preview_payload.pop("results_preview", None)
                preview_json = json.dumps(preview_payload, indent=2, ensure_ascii=False)
            
            # Intentionally do NOT place file_url in DataBlock.urls.
            # DataBlock.urls triggers ClaudeFilePipeline which would attach the large JSON back
            # into the model context and negate the spill-to-file safeguard.
            return DataBlock(text=preview_json)
        except Exception as exc:
            logger.bind(ctx=ctx, code=AppCode.CLAUDE).warning("Failed spilling large {} payload to file: {}", tool_name, exc)
            truncated = json_text[:max_chars] + "\n...[truncated]"
            return DataBlock(text=truncated)

    @staticmethod
    def _next_tool_result_sequence(chat_req: ChatReqClaude) -> int:
        existing_items = getattr(chat_req, "tool_results", None) or []
        return len(existing_items) + 1

    @staticmethod
    def _build_persisted_tool_result(
        *,
        chat_req: ChatReqClaude,
        tool_use_id: str | None,
        name: str,
        source: str,
        step_index: int,
        input_payload: dict[str, Any] | None = None,
        output_text: Any = None,
        is_error: bool = False,
        urls: list[str] | None = None,
        generated_files: list[dict[str, Any]] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved_tool_use_id = tool_use_id
        if not resolved_tool_use_id:
            sequence = AnthropicChat._next_tool_result_sequence(chat_req)
            resolved_tool_use_id = (
                f"{source}-{name}-turn-{chat_req.chat_turn}-step-{step_index}-seq-{sequence}"
            )

        normalized_generated_files: list[dict[str, Any]] = []
        for item in generated_files or []:
            if hasattr(item, "model_dump"):
                normalized_generated_files.append(item.model_dump(exclude_none=True))
            elif isinstance(item, dict):
                normalized_generated_files.append(dict(item))

        normalized_urls: list[str] = []
        seen_urls: set[str] = set()
        for url in urls or []:
            if not url:
                continue
            url = str(url)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            normalized_urls.append(url)

        return {
            "tool_use_id": str(resolved_tool_use_id),
            "name": name,
            "source": source,
            "chat_turn": int(chat_req.chat_turn),
            "step_index": int(step_index),
            "input": dict(input_payload or {}),
            "output_text": AnthropicChat._truncate_tool_output_text(output_text),
            "is_error": bool(is_error),
            "urls": normalized_urls,
            "generated_files": normalized_generated_files,
            "meta": dict(meta or {}),
        }

    @staticmethod
    def _merge_tool_results(
        chat_req: ChatReqClaude,
        new_items: list[dict[str, Any]],
    ) -> None:
        existing_items = getattr(chat_req, "tool_results", None)
        if existing_items is None:
            return

        existing_ids: set[str] = set()
        for raw_item in existing_items:
            if hasattr(raw_item, "model_dump"):
                item = raw_item.model_dump(exclude_none=True)
            elif isinstance(raw_item, dict):
                item = raw_item
            else:
                continue

            tool_use_id = item.get("tool_use_id")
            if tool_use_id:
                existing_ids.add(str(tool_use_id))

        for item in new_items:
            tool_use_id = item.get("tool_use_id")
            if not tool_use_id:
                continue
            if str(tool_use_id) in existing_ids:
                continue

            existing_items.append(item)
            existing_ids.add(str(tool_use_id))

        if len(existing_items) > 2000:
            chat_req.tool_results = existing_items[-2000:]

    @staticmethod
    def _get_available_tool_results(chat_req: ChatReqClaude) -> list[dict[str, Any]]:
        combined: list[dict[str, Any]] = []
        seen: set[str] = set()

        historical_items = getattr(chat_req, "tool_results_history", None) or []
        current_items = getattr(chat_req, "tool_results", None) or []

        for raw_item in list(historical_items) + list(current_items):
            if hasattr(raw_item, "model_dump"):
                item = raw_item.model_dump(exclude_none=True)
            elif isinstance(raw_item, dict):
                item = dict(raw_item)
            else:
                continue

            identity = str(item.get("tool_use_id") or "")
            if not identity or identity in seen:
                continue

            seen.add(identity)
            combined.append(item)

        return combined

    @staticmethod
    def _json_dict_or_none(text: Any) -> dict[str, Any] | None:
        if not isinstance(text, str):
            return None
        try:
            data = json.loads(text)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _looks_like_vector_store_tool_payload(payload: dict[str, Any]) -> bool:
        return any(
            key in payload
            for key in (
                "operation",
                "vector_store_id",
                "vector_store_name",
                "vector_store",
                "indexed_files",
                "successful_files",
                "reused_files",
                "failed_files",
                "batch",
            )
        )

    @classmethod
    def _extract_tool_payload_dict(cls, raw: Any) -> dict[str, Any] | None:
        payload = raw if isinstance(raw, dict) else cls._json_dict_or_none(raw)
        if not isinstance(payload, dict):
            return None

        if cls._looks_like_vector_store_tool_payload(payload):
            return payload

        blocks = payload.get("blocks")
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, dict):
                    continue

                inner_text = block.get("text")
                inner_payload = cls._json_dict_or_none(inner_text)
                if isinstance(inner_payload, dict):
                    if cls._looks_like_vector_store_tool_payload(inner_payload):
                        return inner_payload
                    return inner_payload

        return payload

    @staticmethod
    def _dedupe_urls(urls: list[str] | None) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw_url in urls or []:
            url = str(raw_url or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(url)
        return out

    @classmethod
    def _build_prepare_claude_working_set_output(
        cls,
        *,
        chat_req: ChatReqClaude,
        args: dict[str, Any],
    ) -> DataBlock:
        requested_urls = cls._dedupe_urls(args.get("urls") or args.get("value") or [])

        known_urls: list[str] = []
        known_urls.extend(list(getattr(chat_req, "all_turn_urls", None) or []))
        known_urls.extend(list(getattr(chat_req, "current_turn_urls", None) or []))
        known_urls.extend(list(getattr(chat_req, "current_turn_vector_store_urls", None) or []))
        known_urls.extend(list(getattr(chat_req, "container_candidate_urls", None) or []))

        for raw_item in getattr(chat_req, "generated_files", None) or []:
            item = cls._normalize_item(raw_item)
            if not item:
                continue
            url = item.get("s3_url") or item.get("url")
            if url:
                known_urls.append(str(url))

        for raw_item in getattr(chat_req, "uploaded_files_manifest", None) or []:
            item = cls._normalize_item(raw_item)
            if not item:
                continue
            url = item.get("source_url")
            if url:
                known_urls.append(str(url))

        known_url_set = set(cls._dedupe_urls(known_urls))

        already_attached_urls = [
            url for url in requested_urls
            if cls._is_url_attached_to_current_container(chat_req, url)
        ]
        selected_urls = [
            url for url in requested_urls
            if url in known_url_set and url not in already_attached_urls
        ]
        skipped_unknown_urls = [
            url for url in requested_urls
            if url not in known_url_set
        ]

        container_id = str(getattr(chat_req, "container", None) or "").strip()
        current_container_attached_urls: list[str] = []

        if container_id:
            for raw_item in getattr(chat_req, "container_available_files", None) or []:
                item = cls._normalize_item(raw_item)
                if not item:
                    continue
                if str(item.get("container_id") or "").strip() != container_id:
                    continue

                source_url = str(item.get("source_url") or "").strip()
                if source_url:
                    current_container_attached_urls.append(source_url)

        current_container_attached_urls = cls._dedupe_urls(current_container_attached_urls)
        current_attached_count = len(current_container_attached_urls)

        remaining_capacity = max(0, MAX_CLAUDE_CONTAINER_FILES - current_attached_count)

        # Guard 1: model asked for too many in a single tool call
        if len(selected_urls) > SAFE_MAX_NEW_ATTACHMENTS_PER_CALL:
            payload = {
                "ok": False,
                "error": (
                    f"Too many files requested for claude_container_file_attach in one call. "
                    f"Requested {len(selected_urls)} new files, but the maximum allowed per call is "
                    f"{SAFE_MAX_NEW_ATTACHMENTS_PER_CALL}. Retry with fewer file URLs."
                ),
                "reason": args.get("reason"),
                "selected_urls": [],
                "already_attached_urls": already_attached_urls,
                "skipped_unknown_urls": skipped_unknown_urls,
                "requested_new_files_count": len(selected_urls),
                "max_new_files_per_call": SAFE_MAX_NEW_ATTACHMENTS_PER_CALL,
                "current_container_attached_count": current_attached_count,
                "max_container_files": MAX_CLAUDE_CONTAINER_FILES,
                "remaining_capacity": remaining_capacity,
            }
            return DataBlock(
                text=json.dumps(payload, indent=2, ensure_ascii=False),
                urls=[],
            )

        # Guard 2: would exceed total container capacity
        if len(selected_urls) > remaining_capacity:
            payload = {
                "ok": False,
                "error": (
                    f"Claude container attachment limit would be exceeded. "
                    f"Container already has {current_attached_count} attached file(s), "
                    f"remaining capacity is {remaining_capacity}, but {len(selected_urls)} new files "
                    f"were requested. Retry with fewer file URLs."
                ),
                "reason": args.get("reason"),
                "selected_urls": [],
                "already_attached_urls": already_attached_urls,
                "skipped_unknown_urls": skipped_unknown_urls,
                "requested_new_files_count": len(selected_urls),
                "max_new_files_per_call": SAFE_MAX_NEW_ATTACHMENTS_PER_CALL,
                "current_container_attached_count": current_attached_count,
                "max_container_files": MAX_CLAUDE_CONTAINER_FILES,
                "remaining_capacity": remaining_capacity,
            }
            return DataBlock(
                text=json.dumps(payload, indent=2, ensure_ascii=False),
                urls=[],
            )

        payload = {
            "ok": True,
            "selected_urls": selected_urls,
            "already_attached_urls": already_attached_urls,
            "skipped_unknown_urls": skipped_unknown_urls,
            "reason": args.get("reason"),
            "count": len(selected_urls),
            "current_container_attached_count": current_attached_count,
            "max_container_files": MAX_CLAUDE_CONTAINER_FILES,
            "remaining_capacity": remaining_capacity,
            "max_new_files_per_call": SAFE_MAX_NEW_ATTACHMENTS_PER_CALL,
        }
        return DataBlock(
            text=json.dumps(payload, indent=2, ensure_ascii=False),
            urls=selected_urls,
        )

    @staticmethod
    def _upsert_vector_store_manifest_entry(
        manifest: list[dict[str, Any]],
        *,
        source_url: str | None = None,
        prepared_url: str | None = None,
        vector_store_file_id: str | None = None,
        openai_file_id: str | None = None,
        updates: dict[str, Any],
    ) -> None:
        def _norm(value: Any) -> str | None:
            value = str(value or "").strip()
            return value or None

        source_url = _norm(source_url)
        prepared_url = _norm(prepared_url)
        vector_store_file_id = _norm(vector_store_file_id)
        openai_file_id = _norm(openai_file_id)

        def _matches(item: dict[str, Any]) -> bool:
            if source_url and item.get("source_url") == source_url:
                return True
            if prepared_url and item.get("prepared_url") == prepared_url:
                return True

            existing_vector_store_file_id = _norm(item.get("vector_store_file_id"))
            if vector_store_file_id and existing_vector_store_file_id == vector_store_file_id:
                return True

            existing_openai_file_id = _norm(item.get("openai_file_id"))
            if openai_file_id and existing_openai_file_id == openai_file_id:
                return True

            return False

        normalized_updates = {k: v for k, v in updates.items() if v is not None}

        for item in manifest:
            if _matches(item):
                item.update(normalized_updates)
                if source_url and not item.get("source_url"):
                    item["source_url"] = source_url
                if prepared_url and not item.get("prepared_url"):
                    item["prepared_url"] = prepared_url
                if vector_store_file_id:
                    item["vector_store_file_id"] = vector_store_file_id
                if openai_file_id:
                    item["openai_file_id"] = openai_file_id
                return

        base = {
            "source_url": source_url,
            "prepared_url": prepared_url,
            "vector_store_file_id": vector_store_file_id,
            "openai_file_id": openai_file_id,
        }
        base.update(normalized_updates)
        manifest.append({k: v for k, v in base.items() if v is not None})

    @staticmethod
    def _normalize_manifest_items(items: list[Any] | None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for raw_item in items or []:
            if hasattr(raw_item, "model_dump"):
                out.append(raw_item.model_dump(exclude_none=True))
            elif isinstance(raw_item, dict):
                out.append(dict(raw_item))
        return out

    @classmethod
    def _remove_vector_store_manifest_entries(
        cls,
        *,
        chat_req: ChatReqClaude,
        vector_store_file_ids: list[str] | None,
    ) -> None:
        target_ids = {
            str(x).strip()
            for x in (vector_store_file_ids or [])
            if str(x).strip()
        }
        if not target_ids:
            return

        manifest = cls._normalize_manifest_items(getattr(chat_req, "vector_store_manifest", None))
        filtered_manifest: list[dict[str, Any]] = []

        for item in manifest:
            existing_vector_store_file_id = str(item.get("vector_store_file_id") or "").strip()
            if existing_vector_store_file_id in target_ids:
                continue
            filtered_manifest.append(item)

        chat_req.vector_store_manifest = filtered_manifest

    @classmethod
    def _apply_vector_store_add_payload_to_state(
        cls,
        *,
        chat_req: ChatReqClaude,
        payload: dict[str, Any],
    ) -> None:
        vector_store = payload.get("vector_store") if isinstance(payload.get("vector_store"), dict) else {}
        chat_req.vector_store_id = (
            payload.get("vector_store_id")
            or vector_store.get("id")
            or chat_req.vector_store_id
        )
        chat_req.vector_store_name = (
            payload.get("vector_store_name")
            or vector_store.get("name")
            or chat_req.vector_store_name
        )

        manifest = cls._normalize_manifest_items(getattr(chat_req, "vector_store_manifest", None))
        turn = int(getattr(chat_req, "chat_turn", 0) or 0)

        indexed_files = payload.get("indexed_files", []) or []
        successful_files = payload.get("successful_files", []) or []
        reused_files = payload.get("reused_files", []) or []
        batch = payload.get("batch") if isinstance(payload.get("batch"), dict) else {}
        batch_status = str(batch.get("status") or "").strip().lower()
        default_status = "indexed" if batch_status == "completed" else "pending"

        def _is_durable_url(url: str | None) -> bool:
            value = str(url or "").strip()
            if not value:
                return False
            if value.startswith("file://"):
                return False
            return True

        successful_openai_file_ids_by_source: dict[str, str] = {
            str(item.get("source") or "").strip(): str(
                item.get("openai_file_id") or item.get("file_id") or ""
            ).strip()
            for item in successful_files
            if isinstance(item, dict)
            and str(item.get("source") or "").strip()
            and str(item.get("openai_file_id") or item.get("file_id") or "").strip()
        }

        reused_openai_file_ids_by_source: dict[str, str] = {
            str(item.get("source") or "").strip(): str(
                item.get("openai_file_id") or item.get("file_id") or ""
            ).strip()
            for item in reused_files
            if isinstance(item, dict)
            and str(item.get("source") or "").strip()
            and str(item.get("openai_file_id") or item.get("file_id") or "").strip()
        }

        durable_success_urls: list[str] = []
        indexed_by_url: dict[str, dict[str, Any]] = {}

        for raw_item in indexed_files:
            if not isinstance(raw_item, dict):
                continue

            source_url = str(raw_item.get("source_url") or "").strip()
            prepared_url = str(raw_item.get("prepared_url") or "").strip()
            chosen_url = source_url or prepared_url
            if not _is_durable_url(chosen_url):
                continue

            status = str(raw_item.get("status") or "").strip().lower()
            vector_store_file_id = str(raw_item.get("vector_store_file_id") or "").strip()
            openai_file_id = str(
                raw_item.get("openai_file_id")
                or raw_item.get("source_openai_file_id")
                or ""
            ).strip()

            if status not in {"indexed", "completed", "pending"} and not vector_store_file_id and not openai_file_id:
                continue

            durable_success_urls.append(chosen_url)
            indexed_by_url[chosen_url] = dict(raw_item)

        for source_url, openai_file_id in successful_openai_file_ids_by_source.items():
            if not _is_durable_url(source_url):
                continue
            if source_url not in indexed_by_url:
                durable_success_urls.append(source_url)
                indexed_by_url[source_url] = {
                    "source_url": source_url,
                    "prepared_url": source_url,
                    "openai_file_id": openai_file_id,
                    "vector_store_file_id": None,
                    "status": default_status,
                }

        for source_url, openai_file_id in reused_openai_file_ids_by_source.items():
            if not _is_durable_url(source_url):
                continue
            if source_url not in indexed_by_url:
                durable_success_urls.append(source_url)
                indexed_by_url[source_url] = {
                    "source_url": source_url,
                    "prepared_url": source_url,
                    "openai_file_id": openai_file_id,
                    "vector_store_file_id": None,
                    "status": default_status,
                }

        durable_success_urls = cls._dedupe_urls(durable_success_urls)
        if durable_success_urls:
            cls._merge_all_turn_urls(chat_req, durable_success_urls)

        for source_url in durable_success_urls:
            indexed_entry = indexed_by_url.get(source_url, {})

            prepared_url = str(indexed_entry.get("prepared_url") or source_url).strip() or source_url
            raw_status = str(indexed_entry.get("status") or default_status).strip().lower() or default_status
            status = "indexed" if raw_status in {"indexed", "completed"} else raw_status

            vector_store_file_id = str(indexed_entry.get("vector_store_file_id") or "").strip() or None
            openai_file_id = str(
                indexed_entry.get("openai_file_id")
                or indexed_entry.get("source_openai_file_id")
                or successful_openai_file_ids_by_source.get(source_url)
                or reused_openai_file_ids_by_source.get(source_url)
                or ""
            ).strip() or None

            if not _is_durable_url(source_url):
                continue
            if not _is_durable_url(prepared_url):
                prepared_url = source_url

            cls._upsert_vector_store_manifest_entry(
                manifest,
                source_url=source_url,
                prepared_url=prepared_url,
                vector_store_file_id=vector_store_file_id,
                openai_file_id=openai_file_id,
                updates={
                    "source": "tool_add_files",
                    "representation": (
                        "native_vector_store"
                        if prepared_url == source_url
                        else "converted_txt_vector_store"
                    ),
                    "status": status,
                    "last_error": None,
                    "vector_store_file_id": vector_store_file_id,
                    "openai_file_id": openai_file_id,
                    "last_seen_turn": turn,
                    "last_indexed_turn": turn if status == "indexed" else None,
                },
            )

        chat_req.vector_store_manifest = manifest

    @classmethod
    def _apply_vector_store_tool_state_update(
        cls,
        *,
        chat_req: ChatReqClaude,
        args: dict[str, Any],
        tool_output_str: str,
        step_index: int,
    ) -> None:
        payload = cls._extract_tool_payload_dict(tool_output_str)
        if not payload:
            return

        vector_store = payload.get("vector_store") if isinstance(payload.get("vector_store"), dict) else {}
        chat_req.vector_store_id = (
            payload.get("vector_store_id")
            or vector_store.get("id")
            or chat_req.vector_store_id
        )
        chat_req.vector_store_name = (
            payload.get("vector_store_name")
            or vector_store.get("name")
            or chat_req.vector_store_name
        )

        operation = str(args.get("operation") or "")

        if operation == "delete_vector_store":
            chat_req.vector_store_id = None
            chat_req.vector_store_name = None
            chat_req.vector_store_manifest = []
            chat_req.current_turn_vector_store_urls = []
            return

        if operation == "delete_vector_store_files":
            deleted_vector_store_file_ids = [
                str(x).strip()
                for x in (args.get("delete_file_ids") or [])
                if str(x).strip()
            ]

            cls._remove_vector_store_manifest_entries(
                chat_req=chat_req,
                vector_store_file_ids=deleted_vector_store_file_ids,
            )
            return

        
        if operation == "reindex_files":
            add_result = payload.get("add_result") if isinstance(payload.get("add_result"), dict) else {}

            deleted_vector_store_file_ids = [
                str(x).strip()
                for x in (args.get("delete_file_ids") or [])
                if str(x).strip()
            ]
            cls._remove_vector_store_manifest_entries(
                chat_req=chat_req,
                vector_store_file_ids=deleted_vector_store_file_ids,
            )
            if add_result:
                cls._apply_vector_store_add_payload_to_state(
                    chat_req=chat_req,
                    payload=add_result,
                )
            return

        if operation == "add_files":
            cls._apply_vector_store_add_payload_to_state(
                chat_req=chat_req,
                payload=payload,
            )
            return

    @staticmethod
    def _sanitize_assistant_content_for_server_tool_pairs(
        content: list[Any],
    ) -> list[Any]:

        server_result_types = {
            "bash_code_execution_tool_result",
            "code_execution_tool_result",
            "text_editor_code_execution_tool_result",
        }

        server_tool_names_requiring_result = {
            "bash_code_execution",
            "code_execution",
            "text_editor_code_execution",
        }

        normalized_content: list[tuple[Any, dict[str, Any] | None]] = []

        for block in content:
            if isinstance(block, dict):
                payload = block
            elif hasattr(block, "model_dump"):
                try:
                    payload = block.model_dump(exclude_none=True)
                except Exception:
                    payload = None
            else:
                payload = None

            normalized_content.append((block, payload))

        # Collect all server-tool IDs that have an actual result.
        resolved_tool_use_ids: set[str] = set()

        for _, payload in normalized_content:
            if not isinstance(payload, dict):
                continue

            block_type = str(payload.get("type") or "")

            if block_type not in server_result_types:
                continue

            tool_use_id = payload.get("tool_use_id")

            if tool_use_id:
                resolved_tool_use_ids.add(str(tool_use_id))

        sanitized_content: list[Any] = []

        for original_block, payload in normalized_content:
            if not isinstance(payload, dict):
                sanitized_content.append(original_block)
                continue

            block_type = str(payload.get("type") or "")
            tool_name = str(payload.get("name") or "")
            tool_use_id = payload.get("id")

            is_unresolved_server_tool = (
                block_type in {"server_tool_use", "beta_server_tool_use"}
                and tool_name in server_tool_names_requiring_result
                and tool_use_id
                and str(tool_use_id) not in resolved_tool_use_ids
            )

            if is_unresolved_server_tool:
                logger.bind(code=AppCode.CLAUDE).warning(
                    "Dropping unresolved Claude server tool use "
                    "tool_use_id={} name={}",
                    tool_use_id,
                    tool_name,
                )
                continue

            sanitized_content.append(original_block)

        return sanitized_content

    @staticmethod
    def _message_contains_compaction(message: dict[str, Any]) -> bool:
        content = message.get("content")
        if not isinstance(content, list):
            return False
        return any(isinstance(block, dict) and block.get("type") == "compaction" for block in content)

    @staticmethod
    def _message_is_tool_result_turn(message: dict[str, Any]) -> bool:
        content = message.get("content")
        if not isinstance(content, list) or not content:
            return False
        return all(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)

    def _trim_messages_for_context(
        self,
        *,
        messages: list[dict[str, Any]],
        keep_last_messages: int,
    ) -> list[dict[str, Any]]:
        if len(messages) <= keep_last_messages:
            return messages

        target_start = max(0, len(messages) - keep_last_messages)

        for idx in range(target_start, len(messages)):
            if self._message_contains_compaction(messages[idx]):
                logger.bind(code=AppCode.CLAUDE).info("Trimming message history at assistant compaction block index={}", idx)
                return messages[idx:]

        start = target_start
        if start > 0 and messages[start].get("role") == "assistant":
            start -= 1

        if start > 0 and self._message_is_tool_result_turn(messages[start]):
            start -= 1

        logger.bind(code=AppCode.CLAUDE).warning(
            "Applying turn-aware fallback trim old_count={} new_count={} start_index={}",
            len(messages),
            len(messages[start:]),
            start,
        )
        return messages[start:]

    @staticmethod
    def _content_block_type(block: Any) -> str | None:
        block_type = getattr(block, "type", None)
        if isinstance(block_type, str):
            return block_type

        if hasattr(block, "model_dump"):
            try:
                payload = block.model_dump(exclude_none=True)
                if isinstance(payload, dict):
                    payload_type = payload.get("type")
                    if isinstance(payload_type, str):
                        return payload_type
            except Exception:
                return None

        return None

    @staticmethod
    def _usage_to_dict(usage: Any) -> dict[str, Any]:
        if usage is None:
            return {}
        if hasattr(usage, "model_dump"):
            return usage.model_dump(exclude_none=True)
        if isinstance(usage, dict):
            return dict(usage)
        return {}

    def _log_usage(self, *, ctx: Context, chat_completion: Message) -> None:
        usage_payload = self._usage_to_dict(getattr(chat_completion, "usage", None))
        if not usage_payload:
            return

        iterations = usage_payload.get("iterations") or []
        billed_input_tokens = usage_payload.get("input_tokens")
        billed_output_tokens = usage_payload.get("output_tokens")

        if isinstance(iterations, list) and iterations:
            billed_input_tokens = sum(int(item.get("input_tokens", 0) or 0) for item in iterations)
            billed_output_tokens = sum(int(item.get("output_tokens", 0) or 0) for item in iterations)

        logger.bind(ctx=ctx, code=AppCode.CLAUDE).info(
            "Claude usage input_tokens={} output_tokens={} billed_input_tokens={} billed_output_tokens={} "
            "cache_read_input_tokens={} cache_creation_input_tokens={} iterations={}",
            usage_payload.get("input_tokens"),
            usage_payload.get("output_tokens"),
            billed_input_tokens,
            billed_output_tokens,
            usage_payload.get("cache_read_input_tokens"),
            usage_payload.get("cache_creation_input_tokens"),
            len(iterations) if isinstance(iterations, list) else 0,
        )

    @staticmethod
    def _log_response_context_management(*, ctx: Context, chat_completion: Message) -> None:
        response_context_management = getattr(chat_completion, "context_management", None)
        if response_context_management is None:
            return

        if hasattr(response_context_management, "model_dump"):
            response_context_management = response_context_management.model_dump(exclude_none=True)

        applied_edits = []
        applied_edit_types = []
        if isinstance(response_context_management, dict):
            applied_edits = response_context_management.get("applied_edits") or []
            if isinstance(applied_edits, list):
                applied_edit_types = [
                    edit.get("type")
                    for edit in applied_edits
                    if isinstance(edit, dict)
                ]

        logger.bind(ctx=ctx, code=AppCode.CLAUDE).info(
            "Claude response context_management={} applied_edits_count={} applied_edit_types={}",
            response_context_management,
            len(applied_edits) if isinstance(applied_edits, list) else 0,
            applied_edit_types,
        )

    @staticmethod
    def _has_compaction_edit(chat_req: ChatReqClaude) -> bool:
        context_management = getattr(chat_req, "context_management", None)
        edits = getattr(context_management, "edits", None) or []
        return any(getattr(edit, "type", None) == "compact_20260112" for edit in edits)

    @staticmethod
    def _compaction_pause_enabled(chat_req: ChatReqClaude) -> bool:
        context_management = getattr(chat_req, "context_management", None)
        edits = getattr(context_management, "edits", None) or []
        for edit in edits:
            if getattr(edit, "type", None) == "compact_20260112" and bool(
                getattr(edit, "pause_after_compaction", False)
            ):
                return True
        return False

    @staticmethod
    def _extract_stream_error_message(event: Any) -> str:
        error_obj = getattr(event, "error", None)

        if error_obj is None and hasattr(event, "model_dump"):
            try:
                payload = event.model_dump(exclude_none=True)
                if isinstance(payload, dict):
                    error_obj = payload.get("error")
            except Exception:
                error_obj = None

        if isinstance(error_obj, dict):
            return str(
                error_obj.get("message")
                or error_obj.get("type")
                or "Anthropic streaming error"
            )

        message = getattr(error_obj, "message", None)
        if message:
            return str(message)

        error_type = getattr(error_obj, "type", None)
        if error_type:
            return str(error_type)

        return "Anthropic streaming error"

    @staticmethod
    def _notify_stream_text(
        *,
        ctx: Context,
        notify: Notify | None,
        text: str,
    ) -> None:
        if not notify or not text:
            return

        try:
            notify(ctx, text)
        except Exception as exc:
            logger.bind(ctx=ctx, code=AppCode.CLAUDE).debug("Failed forwarding Claude stream text chunk: {}", exc)

    @staticmethod
    def _notify_status(
        *,
        ctx: Context,
        notify: Notify | None,
        message: str,
    ) -> None:
        if not notify:
            return

        try:
            notify(ctx, f"{message}")
        except Exception as exc:
            logger.bind(ctx=ctx, code=AppCode.CLAUDE).debug("Failed sending Claude status notify: {}", exc)
    

    @staticmethod
    def _notify_tool_args(
        *,
        ctx: Context,
        notify: Notify | None,
        streamer: ToolArgsStreamer,
    ) -> None:
        if not notify or not streamer.has_updates():
            return

        try:
            notify(ctx, streamer.render())
        except Exception as exc:
            logger.bind(ctx=ctx, code=AppCode.CLAUDE).debug("Failed sending Claude tool args notify: {}", exc)
        finally:
            streamer.mark_clean()

    @staticmethod
    def _notify_tool_event(
        *,
        ctx: Context,
        notify: Notify | None,
        kind: str,
        name: str,
        payload: Any = None,
        max_chars: int = 3500,
    ) -> None:
        if not notify:
            return

        try:
            message = f"[tool:{kind}] {name}"
            if payload is not None:
                try:
                    payload_text = json.dumps(payload, ensure_ascii=False, default=str)
                except Exception:
                    payload_text = str(payload)
                message = f"{message} {payload_text}"
            if len(message) > max_chars:
                message = message[:max_chars] + " ...[truncated]"
            notify(ctx, message)
        except Exception as exc:
            logger.bind(ctx=ctx, code=AppCode.CLAUDE).debug("Failed sending Claude tool notify: {}", exc)

    @staticmethod
    def _compute_safe_input_token_limit(
        *,
        max_context_window: int | None,
        max_output_tokens: int | None,
    ) -> int | None:
        if not max_context_window:
            return None

        reserved_output = max(0, int(max_output_tokens or 0))
        safety_margin = max(8_192, min(65_536, int(max_context_window * 0.03)))
        safe_limit = max_context_window - reserved_output - safety_margin
        return max(1, safe_limit)

    async def _recount_context_state(
        self,
        *,
        ctx: Context,
        chat_req: ChatReqClaude,
    ):
        preflight = await ClaudeTokenCountingHelper.count(
            client=self.client,
            ctx=ctx,
            chat_req=chat_req,
        )
        decision = ClaudeContextPolicy.decide(
            ClaudeContextPolicyInput(
                model=str(chat_req.model),
                provider=chat_req.provider,
                max_tokens=chat_req.max_tokens,
                messages=chat_req.messages,
                system=chat_req.system,
                tools_count=len(chat_req.tools or []),
                exact_input_tokens=preflight.input_tokens
                if preflight.source != "skipped"
                else None,
                original_input_tokens=preflight.original_input_tokens,
                token_count_source=preflight.source,
            )
        )
        return preflight, decision


    async def _ensure_request_fits_budget(
        self,
        *,
        ctx: Context,
        chat_req: ChatReqClaude,
        context_decision: Any,
    ):
        safe_limit = self._compute_safe_input_token_limit(
            max_context_window=context_decision.max_context_window,
            max_output_tokens=chat_req.max_tokens,
        )
        if safe_limit is None:
            return None, context_decision

        if context_decision.estimated_input_tokens <= safe_limit:
            return None, context_decision

        original_messages = list(chat_req.messages)
        original_count = len(original_messages)
        keep_candidates: list[int] = []
        for keep in [
            context_decision.keep_last_messages,
            max(4, original_count // 2),
            18,
            12,
            8,
            6,
            4,
            2,
        ]:
            if keep < original_count and keep not in keep_candidates:
                keep_candidates.append(keep)

        best_preflight = None
        best_decision = context_decision
        best_messages = original_messages

        for keep_last_messages in keep_candidates:
            candidate_messages = self._trim_messages_for_context(
                messages=original_messages,
                keep_last_messages=keep_last_messages,
            )
            if len(candidate_messages) >= original_count:
                continue

            chat_req.messages = candidate_messages
            candidate_preflight, candidate_decision = await self._recount_context_state(
                ctx=ctx,
                chat_req=chat_req,
            )

            logger.bind(ctx=ctx, code=AppCode.CLAUDE).warning(
                "Trimmed Claude request history old_messages={} new_messages={} keep_last_messages={} "
                "input_tokens={} safe_limit={}",
                original_count,
                len(candidate_messages),
                keep_last_messages,
                candidate_decision.estimated_input_tokens,
                safe_limit,
            )

            if candidate_decision.estimated_input_tokens < best_decision.estimated_input_tokens:
                best_preflight = candidate_preflight
                best_decision = candidate_decision
                best_messages = candidate_messages

            if candidate_decision.estimated_input_tokens <= safe_limit:
                return candidate_preflight, candidate_decision

        chat_req.messages = best_messages
        if best_decision.estimated_input_tokens > safe_limit:
            raise HTTPException(
                status_code=413,
                detail=(
                    "Claude request is too large even after compaction-aware trimming. "
                    f"input_tokens={best_decision.estimated_input_tokens} safe_limit={safe_limit}"
                ),
            )
        return best_preflight, best_decision

    @staticmethod
    def _tool_display_name(name: str) -> str:
        mapping = {
            "code_execution": "code interpreter",
            "memory": "memory",
            "web_search": "web search",
            "web_fetch": "web fetch",
        }
        return mapping.get(name, name)


    async def _create_or_stream_message(
        self,
        *,
        ctx: Context,
        app_state: AppState,
        chat_req: ChatReqClaude,
        api_params: dict[str, Any],
        container_id: str | None,
        notify: Notify | None,
    ) -> Message:
        use_beta = api_params.get("betas") is not None
        use_stream = bool(chat_req.stream)
        defer_stream_text_until_final = str(chat_req.model).startswith("claude-fable-5")
        sampling_params = {
            name: api_params.pop(name)
            for name in ("temperature", "top_p", "top_k")
            if name in api_params
        }
        if sampling_params:
            api_params["extra_body"] = {**api_params.get("extra_body", {}), **sampling_params}
        notify_main = (
            Notify(topic=text_topic(notify.topic), payload=NotifyPayload.snapshot) if notify else None
        )

        if use_stream:
            self._notify_status(
                ctx=ctx,
                notify=notify,
                message=f"Starting Claude stream on model {chat_req.model}",
            )

            logger.bind(
                ctx=ctx,
                code=AppCode.CLAUDE,
                model=chat_req.model,
                provider=chat_req.provider,
                beta=use_beta,
            ).info("Claude streaming request started")

            if use_beta:
                stream_manager = (
                    self.client.beta.messages.stream(container=container_id, **api_params)
                    if container_id
                    else self.client.beta.messages.stream(**api_params)
                )
            else:
                stream_manager = self.client.messages.stream(**api_params)

            stream_text = ""
            tool_use_id_to_name: dict[str, str] = {}
            tool_args_streamer: ToolArgsStreamer | None = None

            async with stream_manager as stream:
                async for event in stream:
                    event_type = getattr(event, "type", None)

                    if event_type == "text":
                        chunk = getattr(event, "text", "") or ""
                        if chunk:
                            stream_text += chunk
                            if not defer_stream_text_until_final:
                                self._notify_stream_text(
                                    ctx=ctx, notify=notify_main, text=stream_text
                                )
                        continue

                    if event_type == "input_json":
                        if tool_args_streamer is not None:
                            tool_args_streamer.ingest(getattr(event, "partial_json", "") or "")
                            self._notify_tool_args(
                                ctx=ctx, notify=notify, streamer=tool_args_streamer
                            )
                        continue

                    if event_type == "error":
                        error_message = self._extract_stream_error_message(event)
                        self._notify_status(
                            ctx=ctx,
                            notify=notify,
                            message=f"Claude stream error: {error_message}",
                        )
                        logger.bind(ctx=ctx, code=AppCode.CLAUDE).error("Claude streaming error event: {}", error_message)
                        raise HTTPException(status_code=529, detail=error_message)

                    if event_type == "message_start":
                        self._notify_status(
                            ctx=ctx,
                            notify=notify,
                            message="Claude started responding",
                        )
                        continue

                    if event_type == "message_stop":
                        self._notify_status(
                            ctx=ctx,
                            notify=notify,
                            message="Claude finished streaming response",
                        )
                        continue

                    if event_type == "content_block_start":
                        content_block = getattr(event, "content_block", None)
                        if content_block is not None:
                            if hasattr(content_block, "model_dump"):
                                payload = content_block.model_dump(exclude_none=True)
                            elif isinstance(content_block, dict):
                                payload = dict(content_block)
                            else:
                                payload = {"type": getattr(content_block, "type", None)}
                            block_type = str(payload.get("type") or "")
                            if block_type in {"tool_use", "server_tool_use", "beta_tool_use", "beta_server_tool_use", "mcp_tool_use"}:
                                tool_name = str(payload.get("name") or "")
                                tool_id = str(payload.get("id") or "")
                                if tool_id and tool_name:
                                    tool_use_id_to_name[tool_id] = tool_name
                                tool_args_streamer = ToolArgsStreamer(
                                    tool_name or block_type,
                                    key_char_limit=100,
                                    value_char_limit=100,
                                )
                                self._notify_tool_args(
                                    ctx=ctx, notify=notify, streamer=tool_args_streamer
                                )
                        continue

                    if event_type == "content_block_stop":
                        content_block = getattr(event, "content_block", None)
                        if content_block is not None:
                            if hasattr(content_block, "model_dump"):
                                payload = content_block.model_dump(exclude_none=True)
                            elif isinstance(content_block, dict):
                                payload = dict(content_block)
                            else:
                                payload = {"type": getattr(content_block, "type", None)}
                            block_type = str(payload.get("type") or "")
                            if block_type in {"tool_use", "server_tool_use", "beta_tool_use", "beta_server_tool_use", "mcp_tool_use"}:
                                tool_input = payload.get("input")
                                if tool_args_streamer is not None:
                                    if tool_input:
                                        tool_args_streamer.finalize_from_full(
                                            json.dumps(tool_input, ensure_ascii=False, default=str)
                                        )
                                    self._notify_tool_args(
                                        ctx=ctx, notify=notify, streamer=tool_args_streamer
                                    )
                                    tool_args_streamer = None
                            elif "tool_result" in block_type:
                                tool_use_id = str(payload.get("tool_use_id") or "")
                                tool_name = tool_use_id_to_name.get(tool_use_id) or block_type
                                self._notify_tool_event(
                                    ctx=ctx,
                                    notify=notify,
                                    kind="result",
                                    name=tool_name,
                                    payload=payload,
                                )
                        continue

                    if event_type in {
                        "message_delta",
                        "thinking",
                        "signature_delta",
                        "ping",
                    }:
                        logger.bind(ctx=ctx, code=AppCode.CLAUDE).trace(
                            "Claude streaming event type={}",
                            event_type,
                        )
                        continue
                    
                    # this can be bad because it will add thousands of times so commenting it out. 
                    # logger.bind(ctx=ctx).debug(
                    #     "Claude streaming unknown event type={}",
                    #     event_type,
                    # )

                final_message = await stream.get_final_message()
                final_stop_reason = getattr(final_message, "stop_reason", None)
                if defer_stream_text_until_final and final_stop_reason == "refusal":
                    logger.bind(ctx=ctx, code=AppCode.CLAUDE).warning(
                        "Suppressing deferred Claude stream text because final stop_reason=refusal model={}",
                        chat_req.model,
                    )
                else:
                    self._notify_stream_text(ctx=ctx, notify=notify_main, text=stream_text)

            if hasattr(final_message, "container") and final_message.container:
                chat_req.container = final_message.container.id

            self._notify_status(
                ctx=ctx,
                notify=notify,
                message="Claude stream accumulated into final message",
            )

            logger.bind(ctx=ctx, code=AppCode.CLAUDE).info("Claude streaming request completed")
            return final_message

        self._notify_status(
            ctx=ctx,
            notify=notify,
            message=f"Starting Claude request on model {chat_req.model}",
        )

        if use_beta:
            chat_completion = (
                await self.client.beta.messages.create(container=container_id, **api_params)
                if container_id
                else await self.client.beta.messages.create(**api_params)
            )
            if hasattr(chat_completion, "container") and chat_completion.container:
                chat_req.container = chat_completion.container.id
            self._notify_status(
                ctx=ctx,
                notify=notify,
                message="Claude response received",
            )
            logger.bind(ctx=ctx, code=AppCode.CLAUDE).info("Claude completion beta received")
            return chat_completion

        chat_completion = await self.client.messages.create(**api_params)
        self._notify_status(
            ctx=ctx,
            notify=notify,
            message="Claude response received",
        )
        logger.bind(ctx=ctx, code=AppCode.CLAUDE).info("Claude completion received")
        return chat_completion
    

    @staticmethod
    def _render_generated_file_links(new_files: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for item in new_files:
            file_name = item.get("file_name") or item.get("filename") or "unnamed_file"
            s3_url = item.get("s3_url") or item.get("url")
            if not s3_url:
                continue
            lines.append(f"- {file_name}: {s3_url}")

        return "\n".join(lines) if lines else ""

    

    @staticmethod
    def _strip_internal_chat_req_fields(api_params: dict[str, Any]) -> dict[str, Any]:
        """
        Remove internal orchestration/state fields that are valid in ChatReqClaude
        but must never be forwarded to Anthropic Messages API.
        """
        internal_only_fields = {
            "provider",
            "thinking_mode",
            "thinking_budget",
            "thinking_effort",
            "max_thoughts",
            "prompt_caching",
            "context_management_policy",
            "token_count_preflight",
            "citations",
            "auto_resume_paused_compaction",
            "stream",
            "generated_files",
            "chat_turn",
            "tool_results",
            "tool_results_history",

            # persisted chat history (not sent to Anthropic)
            "visible_turns",
            "pinned_messages",
            "turn_history",

            # conversation working-set / vector-store state
            "vector_store_enabled",
            "vector_store_id",
            "vector_store_name",
            "history_vector_store_enabled",
            "history_vector_store_id",
            "history_vector_store_name",
            "history_vector_store_index_every_n_turns",
            "history_vector_store_last_indexed_turn",
            "current_turn_urls",
            "current_turn_vector_store_urls",
            "current_turn_container_urls",
            "all_turn_urls",
            "vector_store_manifest",
            "container_candidate_urls",
            "container_available_files", 
             "uploaded_files_manifest",
       }

        for field_name in internal_only_fields:
            api_params.pop(field_name, None)

        return api_params
    

    @staticmethod
    def _normalize_item(raw_item: Any) -> dict[str, Any] | None:
        if hasattr(raw_item, "model_dump"):
            return raw_item.model_dump(exclude_none=True)
        if isinstance(raw_item, dict):
            return dict(raw_item)
        return None

    @staticmethod
    def _merge_all_turn_urls(
        chat_req: ChatReqClaude,
        urls: list[str] | None,
    ) -> None:
        existing = [str(x) for x in (getattr(chat_req, "all_turn_urls", None) or [])]
        merged: list[str] = []
        seen: set[str] = set()

        for raw_url in existing + [str(x) for x in (urls or [])]:
            url = str(raw_url or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(url)

        chat_req.all_turn_urls = merged

    @staticmethod
    def _is_url_attached_to_current_container(
        chat_req: ChatReqClaude,
        url: str,
    ) -> bool:
        target_url = str(url or "").strip()
        container_id = str(getattr(chat_req, "container", None) or "").strip()
        if not target_url or not container_id:
            return False

        for raw_item in getattr(chat_req, "container_available_files", None) or []:
            item = AnthropicChat._normalize_item(raw_item)
            if not item:
                continue
            if (
                str(item.get("container_id") or "") == container_id
                and str(item.get("source_url") or "") == target_url
            ):
                return True

        return False

    @staticmethod
    def _build_uploaded_file_id_lookup(
        chat_req: ChatReqClaude,
        urls: list[str] | None = None,
    ) -> dict[str, str]:
        filter_set = {str(x).strip() for x in (urls or []) if str(x).strip()}
        lookup: dict[str, str] = {}

        for raw_item in getattr(chat_req, "uploaded_files_manifest", None) or []:
            item = AnthropicChat._normalize_item(raw_item)
            if not item:
                continue

            source_url = str(item.get("source_url") or "").strip()
            claude_file_id = str(item.get("claude_file_id") or "").strip()

            if not source_url or not claude_file_id:
                continue

            if filter_set and source_url not in filter_set:
                continue

            lookup[source_url] = claude_file_id

        return lookup

    @staticmethod
    def _merge_uploaded_files_manifest(
        *,
        chat_req: ChatReqClaude,
        uploaded_files: list[dict[str, Any]],
    ) -> None:
        existing_items = getattr(chat_req, "uploaded_files_manifest", None)
        if existing_items is None:
            return

        normalized_existing: list[dict[str, Any]] = []
        for raw_item in existing_items:
            item = AnthropicChat._normalize_item(raw_item)
            if item:
                normalized_existing.append(item)

        by_source_url = {
            str(item.get("source_url") or item.get("s3_url") or ""): item
            for item in normalized_existing
            if item.get("source_url") or item.get("s3_url")
        }

        current_turn_urls = set(str(x) for x in (getattr(chat_req, "current_turn_urls", None) or []))
        generated_urls = set()
        for raw_item in getattr(chat_req, "generated_files", None) or []:
            item = AnthropicChat._normalize_item(raw_item)
            if not item:
                continue
            url = item.get("s3_url") or item.get("url")
            if url:
                generated_urls.add(str(url))

        turn = int(getattr(chat_req, "chat_turn", 0) or 0)

        for item in uploaded_files or []:
            source_url = str(item.get("source_url") or item.get("s3_url") or item.get("url") or "").strip()
            claude_file_id = str(item.get("file_id") or "").strip()
            file_name = item.get("file_name") or item.get("filename")
            content_sha256 = str(item.get("content_sha256") or "").strip()
            size_bytes = item.get("size_bytes")

            if not source_url or not claude_file_id:
                continue

            source_kind = "manual_attach"
            if source_url in current_turn_urls:
                source_kind = "current_turn"
            elif source_url in generated_urls:
                source_kind = "generated"

            existing = by_source_url.get(source_url)
            if existing:
                existing["claude_file_id"] = claude_file_id
                existing["file_name"] = file_name or existing.get("file_name")
                existing["last_used_turn"] = turn
                existing["source"] = source_kind
                if content_sha256:
                    existing["content_sha256"] = content_sha256
                if size_bytes is not None:
                    existing["size_bytes"] = size_bytes
            else:
                by_source_url[source_url] = {
                    "source_url": source_url,
                    "claude_file_id": claude_file_id,
                    "file_name": file_name,
                    "first_uploaded_turn": turn,
                    "last_used_turn": turn,
                    "source": source_kind,
                    "content_sha256": content_sha256 or None,
                    "size_bytes": size_bytes,
                }

        chat_req.uploaded_files_manifest = list(by_source_url.values())

    @staticmethod
    def _merge_container_available_files(
        *,
        chat_req: ChatReqClaude,
        uploaded_files: list[dict[str, Any]],
    ) -> None:
        existing_items = getattr(chat_req, "container_available_files", None)
        if existing_items is None:
            return

        container_id = str(getattr(chat_req, "container", None) or "").strip()
        if not container_id:
            return

        normalized_existing: list[dict[str, Any]] = []
        for raw_item in existing_items:
            item = AnthropicChat._normalize_item(raw_item)
            if item:
                normalized_existing.append(item)

        by_key = {
            (str(item.get("container_id") or ""), str(item.get("source_url") or "")): item
            for item in normalized_existing
            if item.get("container_id") and item.get("source_url")
        }

        current_turn_urls = set(str(x) for x in (getattr(chat_req, "current_turn_urls", None) or []))
        generated_urls = set()
        for raw_item in getattr(chat_req, "generated_files", None) or []:
            item = AnthropicChat._normalize_item(raw_item)
            if not item:
                continue
            url = item.get("s3_url") or item.get("url")
            if url:
                generated_urls.add(str(url))

        attached_turn = int(getattr(chat_req, "chat_turn", 0) or 0)

        for item in uploaded_files or []:
            source_url = str(item.get("s3_url") or item.get("url") or "").strip()
            claude_file_id = str(item.get("file_id") or "").strip()
            file_name = item.get("file_name") or item.get("filename")

            if not source_url or not claude_file_id:
                continue

            source_kind = "manual_attach"
            if source_url in current_turn_urls:
                source_kind = "current_turn"
            elif source_url in generated_urls:
                source_kind = "generated"

            by_key[(container_id, source_url)] = {
                "container_id": container_id,
                "source_url": source_url,
                "claude_file_id": claude_file_id,
                "file_name": file_name,
                "attached_turn": attached_turn,
                "source": source_kind,
            }

        chat_req.container_available_files = list(by_key.values())


    @staticmethod
    def _build_synthetic_max_loop_message(
        *,
        chat_req: ChatReqClaude,
        reason: str,
    ) -> Message:
        text = (
            "I stopped because the Claude tool loop limit was reached.\n\n"
            "I could not safely continue calling tools, so this is a partial result based on "
            "the information already available. Some parts may be incomplete.\n\n"
            f"Reason: {reason}"
        )

        return Message.model_construct(
            id=f"synthetic-max-loop-{gen_uuid()}",
            type="message",
            role="assistant",
            model=str(chat_req.model),
            content=[
                TextBlock.model_construct(
                    type="text",
                    text=text,
                )
            ],
            stop_reason="end_turn",
            stop_sequence=None,
            usage=None,
        )

    async def _finalize_after_max_loop(
        self,
        *,
        ctx: Context,
        app_state: AppState,
        chat_req: ChatReqClaude,
        notify: Notify | None,
        max_loop: int,
    ) -> Message:
        logger.bind(ctx=ctx, code=AppCode.CLAUDE).warning(
            "Claude max loop reached; forcing final no-tool response model={} max_loop={} messages={}",
            chat_req.model,
            max_loop,
            len(chat_req.messages),
        )

        self._notify_status(
            ctx=ctx,
            notify=notify,
            message="Claude reached the tool loop limit; generating final response without tools.",
        )

        chat_req.messages.append(
            ClaudeMessageParam(
                role="user",
                content=[
                    {
                        "type": "text",
                        "text": (
                            "The tool loop limit has been reached. Do not call any more tools. "
                            "Give the best final answer possible using only the information already available. "
                            "Clearly separate: "
                            "1) what has already been completed, "
                            "2) what is still incomplete or uncertain, "
                            "3) the recommended next steps. "
                            "At the end, ask the user whether they want to continue from this point. "
                            "Do not claim that you will continue automatically; only explain that continuation requires the user's next message. "
                        ),
                    }
                ],
            )
        )
        chat_req.sanitize_for_model()
        chat_req.messages = self._sanitize_message_history_server_tool_pairs(chat_req.messages)
        final_api_params = chat_req.model_dump(exclude_none=True)
        final_api_params = self._strip_internal_chat_req_fields(final_api_params)

        container_id = final_api_params.pop("container", None)

        # Critical: force final answer, not another tool loop.
        # Keep the tool definitions (so the model's prior context referencing tools/files remains coherent),
        # but explicitly disallow tool usage for this one final response.
        final_api_params["tool_choice"] = {"type": "none"}

        try:
            final_completion = await self._create_or_stream_message(
                ctx=ctx,
                app_state=app_state,
                chat_req=chat_req,
                api_params=final_api_params,
                container_id=container_id,
                notify=notify,
            )
            TokenUsageService.record(
                ctx=ctx,
                surface=LlmSurface.claude,
                app_state=app_state,
                usage=getattr(final_completion, "usage", None),
                model=str(getattr(final_completion, "model", None) or chat_req.model),
                loop=max_loop + 1,
            )
            return final_completion
        except Exception as exc:
            logger.bind(ctx=ctx, code=AppCode.CLAUDE).exception(
                "Failed to generate final response after Claude max loop: {}",
                exc,
            )
            return self._build_synthetic_max_loop_message(
                chat_req=chat_req,
                reason=str(exc),
            )

    @classmethod
    def _sanitize_message_history_server_tool_pairs(
        cls,
        messages: list[Any],
    ) -> list[Any]:
        sanitized_messages: list[Any] = []

        for message in messages:
            if hasattr(message, "role"):
                role = message.role
                message_content = message.content
            elif isinstance(message, dict):
                role = message.get("role")
                message_content = message.get("content")
            else:
                sanitized_messages.append(message)
                continue

            if role != "assistant" or not isinstance(message_content, list):
                sanitized_messages.append(message)
                continue

            sanitized_content = (
                cls._sanitize_assistant_content_for_server_tool_pairs(
                    message_content
                )
            )

            if not sanitized_content:
                continue

            if hasattr(message, "model_copy"):
                sanitized_messages.append(
                    message.model_copy(
                        update={"content": sanitized_content}
                    )
                )
            else:
                updated_message = dict(message)
                updated_message["content"] = sanitized_content
                sanitized_messages.append(updated_message)

        return sanitized_messages

    async def chat_loop(
        self,
        user: UserORM,
        ctx: Context,
        app_state: AppState,
        chat_req: ChatReqClaude,
        db: AsyncIOMotorDatabase | None = None,
        notify: Notify | None = None,
        tool_output_text_to_file_char_limit: int | None = None,
    ) -> Message:
        logger.bind(ctx=ctx, code=AppCode.CLAUDE).info("Starting Claude loop")
        max_loop = 100
        loop = 0
        safe_input_limit: int | None = None
        user_files: UserFiles | None = None

        if user:
            s3 = get_public_s3()
            user_files = UserFiles(user=user, s3=s3)

        chat_req.sanitize_for_model()
        resolved = ClaudeModelRegistry.resolve(str(chat_req.model), chat_req.provider)

        raw_token_preflight = await ClaudeTokenCountingHelper.count(
            client=self.client,
            ctx=ctx,
            chat_req=chat_req,
        )

        context_decision = ClaudeContextPolicy.decide(
            ClaudeContextPolicyInput(
                model=str(chat_req.model),
                provider=chat_req.provider,
                max_tokens=chat_req.max_tokens,
                messages=chat_req.messages,
                system=chat_req.system,
                tools_count=len(chat_req.tools or []),
                exact_input_tokens=raw_token_preflight.input_tokens
                if raw_token_preflight.source != "skipped"
                else None,
                original_input_tokens=raw_token_preflight.original_input_tokens,
                token_count_source=raw_token_preflight.source,
            )
        )

        ClaudeContextManagementHelper.apply(
            chat_req=chat_req,
            context_decision=context_decision,
        )
        ClaudePromptCachingHelper.apply(
            chat_req=chat_req,
            context_decision=context_decision,
            capabilities=resolved.capabilities,
        )

        effective_token_preflight = raw_token_preflight
        if chat_req.context_management is not None:
            effective_token_preflight = await ClaudeTokenCountingHelper.count(
                client=self.client,
                ctx=ctx,
                chat_req=chat_req,
            )
            context_decision = ClaudeContextPolicy.decide(
                ClaudeContextPolicyInput(
                    model=str(chat_req.model),
                    provider=chat_req.provider,
                    max_tokens=chat_req.max_tokens,
                    messages=chat_req.messages,
                    system=chat_req.system,
                    tools_count=len(chat_req.tools or []),
                    exact_input_tokens=effective_token_preflight.input_tokens
                    if effective_token_preflight.source != "skipped"
                    else None,
                    original_input_tokens=(
                        effective_token_preflight.original_input_tokens
                        or raw_token_preflight.original_input_tokens
                        or raw_token_preflight.input_tokens
                    ),
                    token_count_source=effective_token_preflight.source,
                )
            )

        if (
            context_decision.should_trim_old_messages
            and not resolved.capabilities.supports_compaction
            and len(chat_req.messages) > context_decision.keep_last_messages
        ):
            old_count = len(chat_req.messages)
            chat_req.messages = self._trim_messages_for_context(
                messages=chat_req.messages,
                keep_last_messages=context_decision.keep_last_messages,
            )
            logger.bind(ctx=ctx, code=AppCode.CLAUDE).warning(
                "Trimmed messages from {} to {} due to context pressure and unavailable compaction",
                old_count,
                len(chat_req.messages),
            )

        logger.bind(ctx=ctx, code=AppCode.CLAUDE).info(
            "Claude preflight summary model={} effective_input_tokens={} original_input_tokens={} "
            "source={} window={} max_tokens={} compaction_enabled={} prompt_caching={}",
            str(chat_req.model),
            context_decision.estimated_input_tokens,
            context_decision.original_input_tokens,
            context_decision.token_count_source,
            context_decision.max_context_window,
            chat_req.max_tokens,
            self._has_compaction_edit(chat_req),
            bool(chat_req.prompt_caching and chat_req.prompt_caching.enabled),
        )

        safe_input_limit = self._compute_safe_input_token_limit(
            max_context_window=context_decision.max_context_window,
            max_output_tokens=chat_req.max_tokens,
        )
        if safe_input_limit is not None and context_decision.estimated_input_tokens > safe_input_limit:
            logger.bind(ctx=ctx, code=AppCode.CLAUDE).warning(
                "Claude request exceeds safe pre-send budget input_tokens={} safe_limit={} "
                "window={} max_tokens={}",
                context_decision.estimated_input_tokens,
                safe_input_limit,
                context_decision.max_context_window,
                chat_req.max_tokens,
            )
            trimmed_preflight, trimmed_decision = await self._ensure_request_fits_budget(
                ctx=ctx,
                chat_req=chat_req,
                context_decision=context_decision,
            )
            if trimmed_preflight is not None:
                effective_token_preflight = trimmed_preflight
            context_decision = trimmed_decision

        def _assistant_block_to_request_block(block: Any) -> dict[str, Any] | None:
            if isinstance(block, (TextBlock, BetaTextBlock)):
                return {
                    "type": "text",
                    "text": block.text,
                }

            if isinstance(block, (ToolUseBlock, BetaToolUseBlock)):
                return block.model_dump(exclude_none=True)

            if isinstance(block, (ServerToolUseBlock, BetaServerToolUseBlock)):
                return block.model_dump(exclude_none=True)

            if hasattr(block, "model_dump"):
                payload = block.model_dump(exclude_none=True)
                if payload.get("type") == "text":
                    payload = {
                        "type": "text",
                        "text": payload.get("text", ""),
                    }
                return payload

            return None

        file_urls_text = ""

        while loop < max_loop:
            app_state.raise_if_cancelled()
            snapshot_claude(ctx, app_state, chat_req, loop)
            self._deliver_instructions(ctx, chat_req, await drain_instructions(app_state), notify)
            loop += 1
            chat_req.sanitize_for_model()
            chat_req.messages = self._sanitize_message_history_server_tool_pairs(chat_req.messages)
            api_params = chat_req.model_dump(exclude_none=True)
            api_params = self._strip_internal_chat_req_fields(api_params)

            use_beta = api_params.get("betas") is not None
            use_stream = bool(chat_req.stream)

            if chat_req.computer_use is not None:
                api_params.pop("computer_use", None)

                target_callable = None
                if use_stream:
                    target_callable = (
                        self.client.beta.messages.stream
                        if use_beta
                        else self.client.messages.stream
                    )
                else:
                    target_callable = (
                        self.client.beta.messages.create
                        if use_beta
                        else self.client.messages.create
                    )

                if "computer_use" in inspect.signature(target_callable).parameters:
                    api_params["computer_use"] = ComputerUseConfigParam(enabled=chat_req.computer_use)
                    logger.bind(ctx=ctx, code=AppCode.CLAUDE).info("Claude computer use enabled={}", chat_req.computer_use)
                else:
                    logger.bind(ctx=ctx, code=AppCode.CLAUDE).warning(
                        "Computer use requested but chosen SDK method does not support computer_use"
                    )

            container_id = api_params.pop("container", None)

            logger.bind(ctx=ctx, code=AppCode.CLAUDE).debug(
                "Claude loop={} stream={} betas={} cache_control={} context_management={}",
                loop,
                use_stream,
                api_params.get("betas"),
                api_params.get("cache_control"),
                api_params.get("context_management"),
            )

            if not use_stream:
                notify(ctx, "Claude thinking...") if notify else None

            try:
                # logger.bind(ctx = ctx).info(f"Chat Request is {chat_req}")
                # logger.bind(ctx = ctx).info(f"Api Params is {api_params}")
                chat_completion = await self._create_or_stream_message(
                    ctx=ctx,
                    app_state=app_state,
                    chat_req=chat_req,
                    api_params=api_params,
                    container_id=container_id,
                    notify=notify,
                )
            except Exception as exc:
                if _is_context_window_error_text(str(exc)) and len(chat_req.messages) > 2:
                    logger.bind(ctx=ctx, code=AppCode.CLAUDE).warning(
                        "Claude rejected request for context size; trimming request-time history and retrying once. "
                        "messages={} error={}",
                        len(chat_req.messages),
                        str(exc),
                    )
                    aggressively_trimmed = self._trim_messages_for_context(
                        messages=chat_req.messages,
                        keep_last_messages=max(2, min(8, context_decision.keep_last_messages)),
                    )
                    if len(aggressively_trimmed) < len(chat_req.messages):
                        chat_req.messages = aggressively_trimmed
                        effective_token_preflight, context_decision = await self._recount_context_state(
                            ctx=ctx,
                            chat_req=chat_req,
                        )
                        safe_input_limit = self._compute_safe_input_token_limit(
                            max_context_window=context_decision.max_context_window,
                            max_output_tokens=chat_req.max_tokens,
                        )
                        if (
                            safe_input_limit is not None
                            and context_decision.estimated_input_tokens > safe_input_limit
                        ):
                            trimmed_preflight, trimmed_decision = await self._ensure_request_fits_budget(
                                ctx=ctx,
                                chat_req=chat_req,
                                context_decision=context_decision,
                            )
                            if trimmed_preflight is not None:
                                effective_token_preflight = trimmed_preflight
                            context_decision = trimmed_decision
                        continue
                raise

            self._log_usage(ctx=ctx, chat_completion=chat_completion)
            self._log_response_context_management(ctx=ctx, chat_completion=chat_completion)
            TokenUsageService.record(
                ctx=ctx,
                surface=LlmSurface.claude,
                app_state=app_state,
                usage=getattr(chat_completion, "usage", None),
                model=str(getattr(chat_completion, "model", None) or chat_req.model),
                loop=loop,
            )

            usage_payload = self._usage_to_dict(getattr(chat_completion, "usage", None))
            iterations = usage_payload.get("iterations") or []
            iterations_count = len(iterations) if isinstance(iterations, list) else 0

            content: list[dict[str, Any]] = []
            tool_used = False
            server_tool_result_present = False
            compaction_blocks_seen = 0
            unknown_blocks_seen: list[str] = []

            for single_content in chat_completion.content:
                block_type = self._content_block_type(single_content)

                logger.bind(ctx=ctx, code=AppCode.CLAUDE).debug(
                    "Claude content block class={} type={}",
                    type(single_content).__name__,
                    block_type,
                )

                if block_type == "compaction":
                    compaction_blocks_seen += 1
                    logger.bind(ctx=ctx, code=AppCode.CLAUDE).info(
                        "Claude compaction block received loop={} stop_reason={} iterations={}",
                        loop,
                        getattr(chat_completion, "stop_reason", None),
                        iterations_count,
                    )

                    request_block = _assistant_block_to_request_block(single_content)
                    if request_block is not None:
                        content.append(request_block)
                    continue

                match single_content:
                    case TextBlock():
                        request_block = _assistant_block_to_request_block(single_content)
                        if request_block is not None:
                            content.append(request_block)

                    case BetaTextBlock():
                        request_block = _assistant_block_to_request_block(single_content)
                        if request_block is not None:
                            content.append(request_block)

                    case ToolUseBlock():
                        request_block = _assistant_block_to_request_block(single_content)
                        if request_block is not None:
                            content.append(request_block)

                        self._notify_status(
                            ctx=ctx,
                            notify=notify,
                            message=f"Claude requested tool: {single_content}",
                        )

                        tool_result_envelope = await self.get_tool_result_block_param_from_tool_use_block(
                            ctx=ctx,
                            app_state=app_state,
                            user=user,
                            tool_use_block=single_content,
                            chat_req=chat_req,
                            step_index=loop,
                            db=db,
                            notify=notify,
                            tool_output_text_to_file_char_limit=tool_output_text_to_file_char_limit,
                        )
                        if tool_result_envelope and tool_result_envelope.get("persisted_tool_result"):
                            self._merge_tool_results(
                                chat_req,
                                [tool_result_envelope["persisted_tool_result"]],
                            )

                        sanitized_content = self._sanitize_assistant_content_for_server_tool_pairs(content)
                        if sanitized_content:
                            chat_req.messages.append(
                                ClaudeMessageParam(
                                    content=sanitized_content,
                                    role="assistant",
                                )
                            )

                        if tool_result_envelope is not None:
                            user_content_blocks = [tool_result_envelope["tool_result_block"]]
                            if tool_result_envelope["extra_user_content_blocks"]:
                                user_content_blocks.extend(tool_result_envelope["extra_user_content_blocks"])

                            chat_req.messages.append(
                                ClaudeMessageParam(
                                    content=user_content_blocks,
                                    role="user",
                                )
                            )

                        content = []
                        tool_used = True

                    case BetaToolUseBlock():
                        request_block = _assistant_block_to_request_block(single_content)
                        if request_block is not None:
                            content.append(request_block)

                        self._notify_status(
                            ctx=ctx,
                            notify=notify,
                            message=f"Claude requested tool: {single_content}",
                        )

                        tool_result_envelope = await self.get_tool_result_block_param_from_tool_use_block(
                            ctx=ctx,
                            app_state=app_state,
                            user=user,
                            tool_use_block=single_content,
                            chat_req=chat_req,
                            step_index=loop,
                            db=db,
                            notify=notify,
                            tool_output_text_to_file_char_limit=tool_output_text_to_file_char_limit,
                        )
                        if tool_result_envelope and tool_result_envelope.get("persisted_tool_result"):
                            self._merge_tool_results(
                                chat_req,
                                [tool_result_envelope["persisted_tool_result"]],
                            )

                        sanitized_content = self._sanitize_assistant_content_for_server_tool_pairs(content)

                        if sanitized_content:
                            chat_req.messages.append(
                                ClaudeMessageParam(
                                    content=sanitized_content,
                                    role="assistant",
                                )
                            )

                        if tool_result_envelope is not None:
                            user_content_blocks = [tool_result_envelope["tool_result_block"]]
                            if tool_result_envelope["extra_user_content_blocks"]:
                                user_content_blocks.extend(tool_result_envelope["extra_user_content_blocks"])

                            chat_req.messages.append(
                                ClaudeMessageParam(
                                    content=user_content_blocks,
                                    role="user",
                                )
                            )

                        content = []
                        tool_used = True

                    case ThinkingBlock():
                        content.append(single_content.model_dump())

                    case BetaThinkingBlock():
                        content.append(single_content.model_dump())

                    case RedactedThinkingBlock():
                        content.append(single_content.model_dump())

                    case BetaRedactedThinkingBlock():
                        content.append(single_content.model_dump())

                    case WebSearchToolResultBlock():
                        server_tool_result_present = True
                        request_block = _assistant_block_to_request_block(single_content)
                        self._notify_status(
                            ctx=ctx,
                            notify=notify,
                            message="Claude performing web search",
                        )
                        if request_block is not None:
                            content.append(request_block)
                        self._merge_tool_results(
                            chat_req,
                            [
                                self._build_persisted_tool_result(
                                    chat_req=chat_req,
                                    tool_use_id=getattr(single_content, "tool_use_id", None),
                                    name="web_search",
                                    source="server",
                                    step_index=loop,
                                    input_payload={},
                                    output_text=request_block,
                                    is_error=False,
                                    urls=[],
                                    generated_files=[],
                                    meta={"block_type": block_type},
                                )
                            ],
                        )

                    case BetaWebSearchToolResultBlock():
                        server_tool_result_present = True
                        request_block = _assistant_block_to_request_block(single_content)
                        self._notify_status(
                            ctx=ctx,
                            notify=notify,
                            message="Claude performing web search",
                        )
                        if request_block is not None:
                            content.append(request_block)
                        self._merge_tool_results(
                            chat_req,
                            [
                                self._build_persisted_tool_result(
                                    chat_req=chat_req,
                                    tool_use_id=getattr(single_content, "tool_use_id", None),
                                    name="web_search_beta",
                                    source="server",
                                    step_index=loop,
                                    input_payload={},
                                    output_text=request_block,
                                    is_error=False,
                                    urls=[],
                                    generated_files=[],
                                    meta={"block_type": block_type},
                                )
                            ],
                        )

                    case BetaWebFetchToolResultBlock():
                        server_tool_result_present = True
                        request_block = _assistant_block_to_request_block(single_content)
                        self._notify_status(
                            ctx=ctx,
                            notify=notify,
                            message="Claude performing web fetch",
                        )
                        if request_block is not None:
                            content.append(request_block)
                        self._merge_tool_results(
                            chat_req,
                            [
                                self._build_persisted_tool_result(
                                    chat_req=chat_req,
                                    tool_use_id=getattr(single_content, "tool_use_id", None),
                                    name="beta_web_fetch",
                                    source="server",
                                    step_index=loop,
                                    input_payload={},
                                    output_text=request_block,
                                    is_error=False,
                                    urls=[],
                                    generated_files=[],
                                    meta={"block_type": block_type},
                                )
                            ],
                        )

                    case BetaTextEditorCodeExecutionToolResultBlock():
                        server_tool_result_present = True
                        request_block = _assistant_block_to_request_block(single_content)
                        self._notify_status(
                            ctx=ctx,
                            notify=notify,
                            message="Claude performing web fetch",
                        )
                        if request_block is not None:
                            content.append(request_block)
                            self._merge_tool_results(
                                chat_req,
                                [
                                    self._build_persisted_tool_result(
                                        chat_req=chat_req,
                                        tool_use_id=getattr(single_content, "tool_use_id", None),
                                        name="text_editor_code_execution",
                                        source="server",
                                        step_index=loop,
                                        input_payload={},
                                        output_text=request_block,
                                        is_error=False,
                                        urls=[],
                                        generated_files=[],
                                        meta={"block_type": block_type},
                                    )
                                ],
                            )

                    case BetaBashCodeExecutionToolResultBlock():
                        server_tool_result_present = True
                        generated_files: list[dict[str, Any]] = []
                        if user_files is not None:
                            output_text, generated_files = await self.process_urls_claude(
                                ctx=ctx,
                                client=self.client,
                                user_files=user_files,
                                single_content=single_content,
                            )
                            if generated_files:
                                self._merge_generated_files(chat_req, generated_files)
                                links_text = self._render_generated_file_links(generated_files)
                                if links_text:
                                    if not file_urls_text:
                                        file_urls_text = "Generated files:\n"
                                    else:
                                        file_urls_text += "\n\n"
                                    file_urls_text += links_text

                        self._notify_status(
                            ctx=ctx,
                            notify=notify,
                            message="Claude code interpreter request completed.",
                        )

                        request_block = _assistant_block_to_request_block(single_content)
                        if request_block is not None:
                            content.append(request_block)

                        if generated_files:
                            content.append(
                                {"type": "text", "text": self._render_generated_files_context(generated_files)}
                            )

                        if request_block is not None:
                            self._merge_tool_results(
                                chat_req,
                                [
                                    self._build_persisted_tool_result(
                                        chat_req=chat_req,
                                        tool_use_id=getattr(single_content, "tool_use_id", None),
                                        name="bash_code_execution",
                                        source="server",
                                        step_index=loop,
                                        input_payload={},
                                        output_text=output_text,
                                        is_error=False,
                                        urls=[item.get("s3_url") for item in generated_files if item.get("s3_url")],
                                        generated_files=generated_files,
                                        meta={"block_type": block_type},
                                    )
                                ],
                            )

                    case BetaCodeExecutionToolResultBlock():
                        server_tool_result_present = True
                        generated_files: list[dict[str, Any]] = []
                        if user_files is not None:
                            output_text, generated_files = await self.process_urls_claude(
                                ctx=ctx,
                                client=self.client,
                                user_files=user_files,
                                single_content=single_content,
                            )
                            if generated_files:
                                self._merge_generated_files(chat_req, generated_files)
                                links_text = self._render_generated_file_links(generated_files)
                                if links_text:
                                    if not file_urls_text:
                                        file_urls_text = "Generated files:\n"
                                    else:
                                        file_urls_text += "\n\n"
                                    file_urls_text += links_text

                        request_block = _assistant_block_to_request_block(single_content)
                        if request_block is not None:
                            content.append(request_block)

                        if generated_files:
                            content.append(
                                {"type": "text", "text": self._render_generated_files_context(generated_files)}
                            )
                        if request_block is not None:
                            self._merge_tool_results(
                                chat_req,
                                [
                                    self._build_persisted_tool_result(
                                        chat_req=chat_req,
                                        tool_use_id=getattr(single_content, "tool_use_id", None),
                                        name="code_execution",
                                        source="server",
                                        step_index=loop,
                                        input_payload={},
                                        output_text=output_text,
                                        is_error=False,
                                        urls=[item.get("s3_url") for item in generated_files if item.get("s3_url")],
                                        generated_files=generated_files,
                                        meta={"block_type": block_type},
                                    )
                                ],
                            )

                    case ServerToolUseBlock():
                        request_block = _assistant_block_to_request_block(single_content)
                        self._notify_status(
                            ctx=ctx,
                            notify=notify,
                            message=f"Claude requested tool f{single_content}",
                        )
                        if request_block is not None:
                            content.append(request_block)

                    case BetaServerToolUseBlock():
                        request_block = _assistant_block_to_request_block(single_content)
                        self._notify_status(
                            ctx=ctx,
                            notify=notify,
                            message=f"Claude requested tool f{single_content}",
                        )
                        if request_block is not None:
                            content.append(request_block)

                    case BetaMCPToolUseBlock():
                        # Server-side MCP tool call: Claude executes it via the MCP
                        # connector, not us. Mirror ServerToolUseBlock handling
                        # (append to request history, do not execute locally).
                        request_block = _assistant_block_to_request_block(single_content)
                        self._notify_status(
                            ctx=ctx,
                            notify=notify,
                            message=f"Claude requested MCP tool: {single_content}",
                        )
                        if request_block is not None:
                            content.append(request_block)

                    case BetaMCPToolResultBlock():
                        # Server-side MCP tool result. Mark server_tool_result_present so
                        # the loop returns (instead of continuing) when stop_reason is
                        # tool_use and no client-side tool was used.
                        server_tool_result_present = True
                        request_block = _assistant_block_to_request_block(single_content)
                        if request_block is not None:
                            content.append(request_block)

                    case _:
                        unknown_blocks_seen.append(type(single_content).__name__)
                        logger.bind(ctx=ctx, code=AppCode.CLAUDE).warning(
                            "Unknown Claude content block class={} type={}",
                            type(single_content).__name__,
                            block_type,
                        )

                        if hasattr(single_content, "model_dump"):
                            try:
                                logger.bind(ctx=ctx, code=AppCode.CLAUDE).debug(
                                    "Unknown Claude content block payload={}",
                                    single_content.model_dump(exclude_none=True),
                                )
                            except Exception:
                                logger.bind(ctx=ctx, code=AppCode.CLAUDE).debug(
                                    "Unknown Claude content block payload could not be dumped"
                                )

                        request_block = _assistant_block_to_request_block(single_content)
                        if request_block is not None:
                            content.append(request_block)

            if content:
                sanitized_content = self._sanitize_assistant_content_for_server_tool_pairs(
                    content
                )

                if sanitized_content:
                    chat_req.messages.append(
                        ClaudeMessageParam(
                            content=sanitized_content,
                            role="assistant",
                        )
                    )

                if (
                    sanitized_content
                    and chat_req.prompt_caching
                    and chat_req.prompt_caching.cache_compaction_blocks
                ):
                    chat_req.messages = (
                        ClaudePromptCachingHelper._ensure_compaction_block_caching(
                            chat_req.messages,
                            ClaudePromptCachingHelper._cache_control_from_config(
                                chat_req.prompt_caching
                            ),
                        )
                    )

            # if file_urls_text.strip():
            #     chat_completion.content.append(TextBlock(text=file_urls_text.strip(), type="text"))

            stop_reason = chat_completion.stop_reason

            if compaction_blocks_seen > 0:
                logger.bind(ctx=ctx, code=AppCode.CLAUDE).info(
                    "Claude compaction detected compaction_blocks_seen={} stop_reason={} iterations={} "
                    "effective_input_tokens={} original_input_tokens={}",
                    compaction_blocks_seen,
                    stop_reason,
                    iterations_count,
                    context_decision.estimated_input_tokens,
                    context_decision.original_input_tokens,
                )

            if unknown_blocks_seen:
                logger.bind(ctx=ctx, code=AppCode.CLAUDE).warning(
                    "Claude unknown content blocks seen count={} classes={}",
                    len(unknown_blocks_seen),
                    unknown_blocks_seen,
                )

            logger.bind(ctx=ctx, code=AppCode.CLAUDE).info("Claude stop_reason={}", stop_reason)

            match stop_reason:
                case "end_turn" | "tool_result" | "stop_sequence" | "refusal":
                    if await self._instructions_at_exit(ctx, app_state, chat_req, notify):
                        continue
                    return chat_completion

                case "compaction":
                    if self._compaction_pause_enabled(chat_req) and not chat_req.auto_resume_paused_compaction:
                        logger.bind(ctx=ctx, code=AppCode.CLAUDE).info(
                            "Returning paused compaction response without auto-resume"
                        )
                        return chat_completion

                    logger.bind(ctx=ctx, code=AppCode.CLAUDE).warning(
                        "Received stop_reason=compaction; auto-resuming paused compaction flow"
                    )
                    chat_req.messages.append(
                        ClaudeMessageParam(
                            content=[{"type": "text", "text": "Continue from the compacted context."}],
                            role="user",
                        )
                    )
                    continue

                case "max_tokens":
                    chat_req.messages.append(
                        ClaudeMessageParam(content=[{"type": "text", "text": "Continue"}], role="user")
                    )
                    continue

                case "model_context_window_exceeded":
                    logger.bind(ctx=ctx, code=AppCode.CLAUDE).warning(
                        "Claude reported model_context_window_exceeded compaction_enabled={} provider={} model={}",
                        self._has_compaction_edit(chat_req),
                        chat_req.provider,
                        chat_req.model,
                    )
                    if len(chat_req.messages) > 2:
                        old_count = len(chat_req.messages)
                        chat_req.messages = self._trim_messages_for_context(
                            messages=chat_req.messages,
                            keep_last_messages=max(2, min(8, context_decision.keep_last_messages)),
                        )
                        if len(chat_req.messages) < old_count:
                            effective_token_preflight, context_decision = await self._recount_context_state(
                                ctx=ctx,
                                chat_req=chat_req,
                            )
                            chat_req.messages.append(
                                ClaudeMessageParam(
                                    content=[{"type": "text", "text": "Continue."}],
                                    role="user",
                                )
                            )
                            continue
                    return chat_completion

                case "tool_use":
                    if not tool_used and server_tool_result_present:
                        if await self._instructions_at_exit(ctx, app_state, chat_req, notify):
                            continue
                        return chat_completion
                    continue

                case _:
                    logger.bind(ctx=ctx, code=AppCode.CLAUDE).warning("Unhandled stop_reason: {}", stop_reason)
                    return chat_completion
    
        return await self._finalize_after_max_loop(
            ctx=ctx,
            app_state=app_state,
            chat_req=chat_req,
            notify=notify,
            max_loop=max_loop,
        )
    


    @staticmethod
    def _json_value_or_none(text: Any) -> Any | None:
        if not isinstance(text, str):
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    @classmethod
    def _tool_output_indicates_error(cls, raw: Any) -> bool:
        if raw is None:
            return False

        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                return False

            # Backward-compatible plain-text failure detection
            if stripped[:10].lower().startswith("error"):
                return True

            parsed = cls._json_value_or_none(stripped)
            if parsed is None:
                return False

            return cls._tool_output_indicates_error(parsed)

        if isinstance(raw, list):
            return any(cls._tool_output_indicates_error(item) for item in raw)

        if not isinstance(raw, dict):
            return False

        # Common top-level failure flags
        if raw.get("ok") is False:
            return True
        if raw.get("success") is False:
            return True

        # Common top-level error payloads
        error_value = raw.get("error")
        if isinstance(error_value, str) and error_value.strip():
            return True
        if error_value not in (None, False, "", [], {}):
            return True

        errors_value = raw.get("errors")
        if isinstance(errors_value, str) and errors_value.strip():
            return True
        if isinstance(errors_value, (list, dict)) and errors_value:
            return True

        # Batch/per-item style failures
        for key in ("results", "items", "outputs"):
            value = raw.get(key)
            if isinstance(value, list):
                for item in value:
                    if cls._tool_output_indicates_error(item):
                        return True

        return False

    async def get_tool_result_block_param_from_tool_use_block(
        self,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        tool_use_block: ToolUseBlock | BetaToolUseBlock,
        chat_req: ChatReqClaude | None = None,
        step_index: int = 0,
        db: AsyncIOMotorDatabase | None = None,
        notify: Notify | None = None,
        tool_output_text_to_file_char_limit: int | None = None,
    ) -> ClaudeToolResultEnvelope | None:
        tool_id = tool_use_block.id
        name = tool_use_block.name
        args = tool_use_block.input

        if tool_use_block.type in {"server_tool_use", "beta_server_tool_use", "mcp_tool_use"}:
            logger.bind(ctx=ctx, code=AppCode.CLAUDE).info("Skipping server tool use block")
            return None

        # logger.bind(ctx=ctx, code=AppCode.CLAUDE).info(f"Tool invoked is {name} , args are {str(args)[:200]} and tool id is {tool_id} ")

        # Rich tool notifications for non-streaming mode (in streaming mode, the tool_use block is already streamed).
        if notify and not (chat_req is not None and bool(getattr(chat_req, "stream", False))):
            self._notify_tool_event(
                ctx=ctx,
                notify=notify,
                kind="start",
                name=str(name),
                payload={"tool_use_id": str(tool_id)},
            )
            self._notify_tool_event(
                ctx=ctx,
                notify=notify,
                kind="args",
                name=str(name),
                payload=dict(args or {}),
            )

        tool_output: DataBlock | None = None

        if name == "memory":
            tool_output_str = await self.get_memory_tool_output_message(ctx=ctx, user=user, args=args)
            # logger.bind(ctx=ctx, code=AppCode.CLAUDE).info(f"memory tool output is {tool_output_str}")

        elif name == "claude_container_file_attach":
            try:
                if chat_req is None:
                    raise HTTPException(
                        status_code=400,
                        detail="claude_container_file_attach requires chat request context",
                    )

                tool_output = self._build_prepare_claude_working_set_output(
                    chat_req=chat_req,
                    args=dict(args or {}),
                )
                # logger.bind(ctx=ctx).info(f"claude_container_file_attach tool output is {tool_output}")
                tool_output_str = tool_output.get_llm_prompt(ctx=ctx)
            except Exception as exc:
                logger.bind(ctx=ctx, code=AppCode.CLAUDE, tool=name).exception(
                    "Failed executing claude_container_file_attach: {}",
                    exc,
                )
                tool_output = None
                tool_output_str = f"error : Tool {name} failed with error: {exc}"
        
        elif name == "chat_history_query_claude":
            try:
                if chat_req is None:
                    raise HTTPException(
                        status_code=400,
                        detail="chat_history_query_claude requires chat request context",
                    )

                query_req = ChatHistoryQueryReq.model_validate(dict(args or {}))

                semantic_query = str(getattr(query_req, "semantic_query", None) or "").strip()
                history_vector_store_id = getattr(chat_req, "history_vector_store_id", None)

                if semantic_query:
                    if not history_vector_store_id:
                        query_payload = {
                            "total_count": 0,
                            "returned_count": 0,
                            "results": [],
                            "summary": {
                                "note": (
                                    "No conversation-memory vector store exists yet "
                                    "(nothing indexed so far). Fall back to turn/text_query filtering."
                                ),
                            },
                        }
                    else:
                        from src.autobots.llm.tools.openai_vector_store_tool import (
                            OpenAIVectorStoreToolConfig,
                            OpenAIVectorStoreToolOperation,
                            openai_vector_store_tool,
                        )
                        vs_result = await openai_vector_store_tool(
                            config=OpenAIVectorStoreToolConfig(
                                operation=OpenAIVectorStoreToolOperation.QUERY_VECTOR_STORE,
                                vector_store_id=history_vector_store_id,
                                query=semantic_query,
                                max_num_results=query_req.semantic_max_results,
                            ),
                            ctx=ctx,
                            app_state=app_state,
                            user=user,
                            db=db,
                            notify=notify,
                        )
                        if isinstance(vs_result, Exception):
                            raise vs_result

                        vs_payload: dict[str, Any] = {}
                        if vs_result.blocks and vs_result.blocks[0].text:
                            vs_payload = json.loads(vs_result.blocks[0].text)

                        vs_results = vs_payload.get("results") or []
                        query_payload = {
                            "total_count": len(vs_results),
                            "returned_count": len(vs_results),
                            "results": vs_results,
                            "summary": {
                                "mode": "semantic",
                                "semantic_query": semantic_query,
                                "history_vector_store_id": history_vector_store_id,
                            },
                        }

                    tool_output = await self._maybe_spill_large_query_json_to_file(
                        ctx=ctx,
                        user=user,
                        chat_req=chat_req,
                        tool_name=name,
                        payload=query_payload,
                        max_chars=tool_output_text_to_file_char_limit,
                        notify=notify,
                    )
                    tool_output_str = tool_output.get_llm_prompt(ctx=ctx)

                    logger.bind(
                        ctx=ctx,
                        code=AppCode.CLAUDE,
                        tool=name,
                        semantic_query=semantic_query,
                        history_vector_store_id=history_vector_store_id,
                    ).info("Resolved chat_history_query_claude via semantic vector-store search")

                else:
                    normalized_turn_history: list[dict[str, Any]] = []
                    for raw_item in getattr(chat_req, "turn_history", None) or []:
                        if hasattr(raw_item, "model_dump"):
                            item = raw_item.model_dump(exclude_none=True)
                        elif isinstance(raw_item, dict):
                            item = dict(raw_item)
                        else:
                            continue
                        normalized_turn_history.append(item)

                    normalized_pinned: list[dict[str, Any]] = []
                    for raw_item in getattr(chat_req, "pinned_messages", None) or []:
                        if hasattr(raw_item, "model_dump"):
                            item = raw_item.model_dump(exclude_none=True)
                        elif isinstance(raw_item, dict):
                            item = dict(raw_item)
                        else:
                            continue
                        normalized_pinned.append(item)

                    query_req.set_turn_history(
                        turn_history=normalized_turn_history,
                        pinned_messages=normalized_pinned,
                    )

                    query_res = ActionChatHistoryQueryClaude._execute_query(query_req)
                    query_payload = query_res.model_dump(exclude_none=True)
                    tool_output = await self._maybe_spill_large_query_json_to_file(
                        ctx=ctx,
                        user=user,
                        chat_req=chat_req,
                        tool_name=name,
                        payload=query_payload,
                        max_chars=tool_output_text_to_file_char_limit,
                        notify=notify,
                    )
                    tool_output_str = tool_output.get_llm_prompt(ctx=ctx)

                    logger.bind(
                        ctx=ctx,
                        code=AppCode.CLAUDE,
                        tool=name,
                        available_turn_history_count=len(normalized_turn_history),
                    ).info("Injected stored chat turn history into chat_history_query_claude")

            except Exception as exc:
                logger.bind(ctx=ctx, code=AppCode.CLAUDE, tool=name).exception(
                    "Failed executing chat_history_query_claude with injected history: {}",
                    exc,
                )
                tool_output = None
                tool_output_str = f"error : Tool {name} failed with error: {exc}"

        elif name == "tool_results_query_claude":
            try:
                if chat_req is None:
                    raise HTTPException(
                        status_code=400,
                        detail="tool_results_query requires chat request context",
                    )

                query_req = ToolResultsQueryReq.model_validate(dict(args or {}))
                query_req.set_tool_results(self._get_available_tool_results(chat_req))

                query_res = ActionToolResultsQueryClaude._execute_query(query_req)
                query_payload = query_res.model_dump(exclude_none=True)
                tool_output = await self._maybe_spill_large_query_json_to_file(
                    ctx=ctx,
                    user=user,
                    chat_req=chat_req,
                    tool_name=name,
                    payload=query_payload,
                    max_chars=tool_output_text_to_file_char_limit,
                    notify=notify,
                )
                tool_output_str = tool_output.get_llm_prompt(ctx=ctx)

                logger.bind(
                    ctx=ctx,
                    code=AppCode.CLAUDE,
                    tool=name,
                    available_tool_results_count=len(query_req.get_tool_results()),
                ).info("Injected stored tool results into tool_results_query")

            except Exception as exc:
                logger.bind(ctx=ctx, code=AppCode.CLAUDE, tool=name).exception(
                    "Failed executing tool_results_query with injected history: {}",
                    exc,
                )
                tool_output = None
                tool_output_str = f"error : Tool {name} failed with error: {exc}"

        else:
            raw_tool_output = await ToolFactoryClaude(user=user, db=db).run_tool(
                ctx,
                app_state,
                name,
                args,
                notify=notify,
                output_text_to_file_char_limit=tool_output_text_to_file_char_limit,
            )

            if isinstance(raw_tool_output, Exception):
                logger.bind(ctx=ctx, code=AppCode.CLAUDE, tool=name).exception("Claude tool returned exception-like result")
                tool_output_str = f"error : Tool {name} failed with error: {raw_tool_output}"
            elif raw_tool_output is None:
                logger.bind(ctx=ctx, code=AppCode.CLAUDE, tool=name).warning("Claude tool returned no output")
                tool_output_str = f"error : Tool {name} returned no output"
            elif not isinstance(raw_tool_output, DataBlock):
                logger.bind(ctx=ctx, code=AppCode.CLAUDE, tool=name, output_type=type(raw_tool_output).__name__).warning(
                    "Claude tool returned unsupported output type"
                )
                tool_output_str = (
                    f"error : Tool {name} returned unsupported output type "
                    f"{type(raw_tool_output).__name__}"
                )
            else:
                tool_output = raw_tool_output
                try:
                    tool_output_str = tool_output.get_llm_prompt(ctx=ctx)
                except Exception as exc:
                    logger.bind(ctx=ctx, code=AppCode.CLAUDE, tool=name).exception(
                        "Claude tool output could not be converted to prompt: {}", exc
                    )
                    tool_output = None
                    tool_output_str = f"error : Tool {name} output serialization failed: {exc}"

        # Special handling for enable_mcp: inject the MCP server into the in-flight
        # request so its tools become available on the next loop iteration. Mirrors
        # OpenAI Responses' enable_mcp injection. Claude needs both a server def in
        # `mcp_servers` and an `mcp_toolset` entry in `tools` (plus the mcp beta).
        if name == "enable_mcp" and tool_output_str:
            try:
                payload = json.loads(tool_output_str)
                if (
                    isinstance(payload, dict)
                    and payload.get("success")
                    and payload.get("server_name")
                ):
                    server_name = str(payload["server_name"])
                    user_mcp = UserMCP(user=user, db=db)
                    mcp_doc = await user_mcp.get_mcp_by_server_name(
                        ctx=ctx, mcp_server_name=server_name, decrypt=True
                    )
                    if mcp_doc:
                        existing_servers = {
                            (s.get("name") if isinstance(s, dict) else getattr(s, "name", None))
                            for s in (chat_req.mcp_servers or [])
                        }
                        if mcp_doc.server_name not in existing_servers:
                            if chat_req.mcp_servers is None:
                                chat_req.mcp_servers = []
                            chat_req.mcp_servers.append(
                                {
                                    "type": "url",
                                    "url": mcp_doc.server_url,
                                    "name": mcp_doc.server_name,
                                    "authorization_token": mcp_doc.oauth_token,
                                }
                            )

                        existing_toolsets = {
                            (
                                t.get("mcp_server_name")
                                if isinstance(t, dict)
                                else getattr(t, "mcp_server_name", None)
                            )
                            for t in (chat_req.tools or [])
                        }
                        if mcp_doc.server_name not in existing_toolsets:
                            if chat_req.tools is None:
                                chat_req.tools = []
                            chat_req.tools.append(
                                {
                                    "type": "mcp_toolset",
                                    "mcp_server_name": mcp_doc.server_name,
                                }
                            )

                        chat_req.betas = list(
                            dict.fromkeys(
                                (chat_req.betas or []) + ["mcp-client-2025-11-20"]
                            )
                        )

                        logger.bind(ctx=ctx, server_name=server_name).info(
                            f"Dynamically added MCP '{server_name}' to runtime Claude request"
                        )
            except Exception as exc:
                logger.bind(ctx=ctx, tool=name, error=str(exc)).exception(
                    "Failed to inject MCP into runtime Claude tools config"
                )

        logger.bind(ctx=ctx, code=AppCode.CLAUDE, tool=name).info("Ran Claude tool")

        is_error = self._tool_output_indicates_error(tool_output_str)

        if notify:
            self._notify_tool_event(
                ctx=ctx,
                notify=notify,
                kind="result",
                name=str(name),
                payload={
                    "tool_use_id": str(tool_id),
                    "is_error": bool(is_error),
                    "output": tool_output_str,
                },
            )

        persisted_tool_result: dict[str, Any] | None = None
        if chat_req is not None and name == "openai_vector_store_tool":
            try:
                self._apply_vector_store_tool_state_update(
                    chat_req=chat_req,
                    args=dict(args or {}),
                    tool_output_str=tool_output_str,
                    step_index=step_index,
                )
            except Exception as exc:
                logger.bind(ctx=ctx, code=AppCode.CLAUDE, tool=name).warning(
                    "Failed updating in-memory vector store state after tool call: {}",
                    exc,
                )
        if chat_req is not None:
            persisted_tool_result = self._build_persisted_tool_result(
                chat_req=chat_req,
                tool_use_id=tool_id,
                name=name,
                source="memory" if name == "memory" else "custom",
                step_index=step_index,
                input_payload=dict(args or {}),
                output_text=tool_output_str,
                is_error=is_error,
                urls=list(tool_output.urls or []) if tool_output else [],
                generated_files=[],
                meta={
                    "tool_block_type": getattr(tool_use_block, "type", None),
                },
            )

        tool_result_content: list[dict[str, Any]] = [{"type": "text", "text": tool_output_str}]
        extra_user_content_blocks: list[dict[str, Any]] = []

        if tool_output and tool_output.urls and chat_req is not None:
            self._merge_all_turn_urls(chat_req, list(tool_output.urls or []))

            urls_for_pipeline = self._dedupe_urls(list(tool_output.urls or []))

            if name == "claude_container_file_attach":
                urls_for_pipeline = [
                    url for url in urls_for_pipeline
                    if not self._is_url_attached_to_current_container(chat_req, url)
                ]

            if urls_for_pipeline:
                pipeline = ClaudeFilePipeline(
                    client=self.client,
                    provider=chat_req.provider,
                )
                existing_uploaded_files_manifest = []
                for raw_item in getattr(chat_req, "uploaded_files_manifest", None) or []:
                    item = self._normalize_item(raw_item)
                    if item:
                        existing_uploaded_files_manifest.append(item)

                pipeline_result = await pipeline.build_blocks(
                    ctx=ctx,
                    user=user,
                    db=db,
                    urls=urls_for_pipeline,
                    tools=chat_req.tools,
                    model=str(chat_req.model),
                    citations_enabled=bool(chat_req.citations),
                    existing_uploaded_files_manifest=existing_uploaded_files_manifest,
                )

                if pipeline_result.failed_urls:
                    logger.bind(ctx=ctx, code=AppCode.CLAUDE).warning(
                        "Claude file pipeline had partial failures: {}",
                        pipeline_result.failed_urls,
                    )

                tool_result_content.extend(pipeline_result.blocks)

                if pipeline_result.container_upload_blocks:
                    extra_user_content_blocks.extend(pipeline_result.container_upload_blocks)

                chat_req.betas = list(
                    dict.fromkeys((chat_req.betas or []) + pipeline_result.required_betas)
                )

                if pipeline_result.uploaded_files:
                    self._merge_uploaded_files_manifest(
                        chat_req=chat_req,
                        uploaded_files=pipeline_result.uploaded_files,
                    )
                    self._merge_container_available_files(
                        chat_req=chat_req,
                        uploaded_files=pipeline_result.uploaded_files,
                    )

        return {
            "tool_result_block": {
                "tool_use_id": tool_id,
                "type": "tool_result",
                "content": tool_result_content,
                "is_error": is_error,
            },
            "extra_user_content_blocks": extra_user_content_blocks,
            "persisted_tool_result": persisted_tool_result,
        }


    async def upload_file_to_claude_directory(
        self,
        ctx: Context,
        user: UserORM,
        full_path: str,
        filename: str,
        content: str | bytes,
        user_files: UserFiles,
    ) -> None:
        content_type, _ = mimetypes.guess_type(filename)
        if content_type is None:
            content_type = "application/octet-stream"

        tmp = SpooledTemporaryFile(mode="w+b")
        if isinstance(content, str):
            tmp.write(content.encode("utf-8"))
        else:
            tmp.write(content)
        tmp.seek(0)

        upload_file = UploadFile(
            filename=filename,
            file=tmp,
            headers=Headers({"content-type": content_type}),
        )
        try:
            await user_files.upload_files(ctx=ctx, files=[upload_file])
            await upload_file.close()
        except Exception as exc:
            logger.bind(ctx=ctx, code=AppCode.CLAUDE).exception("Error uploading memory file: {}", exc)
            raise

    async def get_memory_tool_output_message(
        self,
        ctx: Context,
        user: UserORM,
        args: BetaMemoryTool20250818Command,
    ) -> str:
        s3 = get_s3()
        claude_user_files = UserFiles(
            user=user,
            s3=s3,
            top_level_folder=SettingsProvider.sget().CLAUDE_USER_FILES_S3_FOLDER,
        )

        command = args.get("command")

        match command:
            case "view":
                path = args.get("path")
                if not path.startswith("/memories"):
                    return f"error : Path must start with /memories, got: {path}"
                if "." not in path:
                    if path != "/memories":
                        return "error : Can only list files in /memories directory. Sub-directories are not supported."
                    files = await claude_user_files.list_files(ctx=ctx)
                    return "Directory: /memories\n" + "\n".join([f"- {file.name}" for file in files])

                filename = path[len("/memories/") :]
                return await claude_user_files.get_file_contents(ctx=ctx, filename=filename)

            case "create":
                path = args.get("path")
                if path == "/memories":
                    return f"Directory created with path {path}"
                if not path.startswith("/memories"):
                    return f"error : Path must start with /memories, got: {path}"
                if "." not in path:
                    return "error : All files must be created inside /memories. Sub-directories are not supported."

                filename = path[len("/memories/") :]
                try:
                    await self.upload_file_to_claude_directory(
                        ctx=ctx,
                        user=user,
                        full_path=path,
                        filename=filename,
                        content=args.get("file_text", ""),
                        user_files=claude_user_files,
                    )
                    return f"File created successfully at {path}"
                except Exception as exc:
                    logger.bind(ctx=ctx, code=AppCode.CLAUDE).exception("Memory create error: {}", exc)
                    return f"error : Faced an error, {str(exc)}"

            case "delete":
                path = args.get("path")
                if path == "/memories":
                    return "error : Cannot delete the /memories directory."
                if not path.startswith("/memories"):
                    return f"error : Path must start with /memories, got: {path}"

                filename = path[len("/memories/") :]
                try:
                    await claude_user_files.delete_files(ctx=ctx, filenames=[filename])
                    return f"File deleted: {path}"
                except Exception as exc:
                    logger.bind(ctx=ctx, code=AppCode.CLAUDE).exception("Memory delete error: {}", exc)
                    return f"error : Faced an error, {str(exc)}"

            case "insert":
                path = args.get("path")
                if not path.startswith("/memories"):
                    return f"error : Path must start with /memories, got: {path}"
                if "." not in path:
                    return "error : Found a directory path. Please provide a file path inside /memories."

                filename = path[len("/memories/") :]
                try:
                    content = await claude_user_files.get_file_contents(ctx=ctx, filename=filename)
                except Exception as exc:
                    logger.bind(ctx=ctx, code=AppCode.CLAUDE).exception("Memory insert read error: {}", exc)
                    return f"error : Faced an error while reading the file, {str(exc)}"

                insert_line = args.get("insert_line")
                insert_text = args.get("insert_text")
                lines = content.splitlines()
                if insert_line < 0 or insert_line > len(lines):
                    return f"error : Insert line {insert_line} is out of range. File has {len(lines)} lines."

                lines.insert(insert_line, (insert_text or "").rstrip("\n"))
                try:
                    await self.upload_file_to_claude_directory(
                        ctx=ctx,
                        user=user,
                        full_path=path,
                        filename=filename,
                        content="\n".join(lines),
                        user_files=claude_user_files,
                    )
                    return f"Text inserted at line {insert_line} in {path}"
                except Exception as exc:
                    logger.bind(ctx=ctx, code=AppCode.CLAUDE).exception("Memory insert write error: {}", exc)
                    return f"error : Faced an error while writing the file, {str(exc)}"

            case "rename":
                old_path = args.get("old_path")
                new_path = args.get("new_path")
                if old_path == "/memories":
                    return "error : Cannot rename the /memories directory."
                if not old_path.startswith("/memories"):
                    return f"error : Path must start with /memories, got: {old_path}"
                if "." not in old_path:
                    return "error : Sub-directories are not supported for renaming."

                old_filename = old_path[len("/memories/") :]
                new_filename = new_path[len("/memories/") :]
                try:
                    old_content = await claude_user_files.get_file_contents(ctx=ctx, filename=old_filename)
                    await claude_user_files.delete_files(ctx=ctx, filenames=[old_filename])
                    await self.upload_file_to_claude_directory(
                        ctx=ctx,
                        user=user,
                        full_path=new_path,
                        filename=new_filename,
                        content=old_content,
                        user_files=claude_user_files,
                    )
                    return f"Renamed {old_path} to {new_path}"
                except Exception as exc:
                    logger.bind(ctx=ctx, code=AppCode.CLAUDE).exception("Memory rename error: {}", exc)
                    return f"error : Faced an error while renaming, {str(exc)}"

            case "str_replace":
                path = args.get("path")
                if path == "/memories":
                    return "error : Cannot do string replace on /memories directory."
                if not path.startswith("/memories"):
                    return f"error : Path must start with /memories, got: {path}"
                if "." not in path:
                    return "error : Sub-directories are not supported for string replace."

                filename = path[len("/memories/") :]
                try:
                    old_content = await claude_user_files.get_file_contents(ctx=ctx, filename=filename)
                except Exception as exc:
                    logger.bind(ctx=ctx, code=AppCode.CLAUDE).exception("Memory str_replace read error: {}", exc)
                    return f"error : Faced an error while reading the file, {str(exc)}"

                old_str = args.get("old_str")
                new_str = args.get("new_str")
                count = old_content.count(old_str)
                if count == 0:
                    return f"error : No occurrences of '{old_str}' found in {path}."

                try:
                    await self.upload_file_to_claude_directory(
                        ctx=ctx,
                        user=user,
                        full_path=path,
                        filename=filename,
                        content=old_content.replace(old_str, new_str),
                        user_files=claude_user_files,
                    )
                    return f"Replaced {count} occurrences of '{old_str}' with '{new_str}' in {path}."
                except Exception as exc:
                    logger.bind(ctx=ctx, code=AppCode.CLAUDE).exception("Memory str_replace write error: {}", exc)
                    return f"error : Faced an error while writing the file, {str(exc)}"

        return "error : Unsupported memory command"

    async def process_urls_claude(
        self,
        ctx: Context,
        client: AsyncAnthropic,
        user_files: UserFiles,
        single_content: BetaCodeExecutionToolResultBlock | BetaBashCodeExecutionToolResultBlock,
    ) -> tuple[str, list[dict[str, Any]]]:
        logger.bind(ctx=ctx, code=AppCode.CLAUDE).debug("Processing code execution result block")
        content_req = single_content.content
        content_dict = content_req.model_dump() if hasattr(content_req, "model_dump") else dict(content_req)

        if content_dict.get("type") == "code_execution_tool_result_error":
            return (
                f"Error in code execution: {content_dict.get('error_code', 'Unknown error')}",
                [],
            )

        return_code = content_dict.get("return_code")
        stderr = content_dict.get("stderr", "") or ""
        stdout = content_dict.get("stdout", "") or ""

        if isinstance(return_code, int) and return_code != 0:
            return (f"Error in code execution: {stderr or 'Unknown error'}", [])

        downloadable_content = content_dict.get("content", [])
        if not isinstance(downloadable_content, list):
            downloadable_content = []

        generated_files: list[dict[str, Any]] = []
        for file_info in downloadable_content:
            file_id = file_info.get("file_id")
            if not file_id:
                continue

            try:
                download_resp = await client.beta.files.download(file_id)
                file_bytes = await download_resp.read()
                file_metadata = await client.beta.files.retrieve_metadata(file_id)
                filename = file_metadata.filename or f"code_execution_output_{gen_uuid()}.bin"

                file_obj = io.BytesIO(file_bytes)
                file_obj.seek(0)
                uploaded = await user_files.upload_binary(
                    ctx=ctx,
                    file_obj=file_obj,
                    filename=filename,
                )
                if uploaded and uploaded[0].url:
                    generated_files.append(
                        {
                            "s3_url": str(uploaded[0].url),
                            "file_id": str(file_id),
                            "file_name": filename,
                        }
                    )
            except Exception as exc:
                logger.bind(ctx=ctx, code=AppCode.CLAUDE).exception("Failed persisting code execution output file: {}", exc)

        if generated_files:
            stdout = stdout.rstrip() + "\n\nGenerated files:\n" + "\n".join(
                f"- {item['file_name']}: {item['s3_url']}" for item in generated_files
            )

        return stdout.strip(), generated_files

    async def yield_chat_chunks(
        self,
        chat_res: AsyncStream[MessageStreamEvent],
    ):
        try:
            async for part in chat_res:
                yield part
        except Exception as exc:
            logger.bind(code=AppCode.CLAUDE).exception("Claude stream error: {}", exc)
