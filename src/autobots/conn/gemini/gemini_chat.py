import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from google.genai import types
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.gemini.chat_memory_tool import (
    GeminiChatMemoryToolReq,
    GeminiChatMemoryToolService,
)
from src.autobots.conn.gemini.chat_model_gemini import (
    GeminiChatConfig,
    GeminiGeneratedFileRef,
    GeminiStoredToolResult,
    GeminiTurnRecord,
    GeminiUploadedFileRef,
    GeminiUsageMetadataRecord,
)
from src.autobots.conn.gemini.file_search_store.file_search_store_auto_ingest_policy import (
    FILE_SEARCH_MAX_DOC_BYTES,
    build_manifest_entry,
    decide_auto_ingest,
    extract_text_fallback,
    is_supported_file_search_mime_type,
    normalize_mime_type,
)
from src.autobots.conn.gemini.file_search_store.file_search_store_auto_ingest_policy import (
    display_name_from_url as policy_display_name_from_url,
)
from src.autobots.conn.gemini.file_search_store.file_search_store_client import (
    GeminiFileSearchStoreClient,
)
from src.autobots.conn.gemini.file_search_store.file_search_store_models import (
    GeminiFileSearchStoreRef,
)
from src.autobots.conn.gemini.file_search_store.file_search_store_prompt import (
    build_file_search_store_context_block,
)
from src.autobots.conn.gemini.file_search_store.file_search_store_state import (
    apply_file_search_store_tool_state_update,
    merge_file_search_store_manifest,
)
from src.autobots.conn.gemini.message_normalizer_gemini import GeminiMessageNormalizer
from src.autobots.conn.gemini.streaming_adapter_gemini import GeminiStreamAccumulator
from src.autobots.conn.gemini.thought_signatures_gemini import GeminiThoughtSignatureHelper
from src.autobots.conn.openai.openai_files.openai_files import OpenaiFiles
from src.autobots.conn.openai.openai_files.openai_files_model import FileCreate
from src.autobots.conn.openai.openai_responses.tool_args_streamer import ToolArgsStreamer
from src.autobots.core.cancellation.run_instructions import RunInstruction, drain_instructions
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context

# NOTE: FileUploader is imported lazily inside legacy helpers to keep this module importable in minimal envs.
from src.autobots.files.file_mime_types import (
    is_generic_binary_mime_type,
    resolve_file_mime_type,
)
from src.autobots.llm.chat_snapshot.provider_snapshots import snapshot_gemini
from src.autobots.llm.tools.openai_vector_store_tool import (
    OpenAIVectorStoreFileInput,
    OpenAIVectorStoreToolConfig,
    OpenAIVectorStoreToolOperation,
    openai_vector_store_tool,
)
from src.autobots.llm.tools.tool_factory_gemini import ToolFactoryGemini
from src.autobots.server_sent_events.server_sent_events import Notify, NotifyPayload
from src.autobots.server_sent_events.topics import text_topic
from src.autobots.token_usage.token_usage_service import LlmSurface, TokenUsageService
from src.autobots.user.user_orm_model import UserORM


class GeminiChatRuntime:
    DEFAULT_TOOL_NAMES = [
        "gemini_context_tool",
        "gemini_code_execution_tool",
        "gemini_chat_memory_tool",
    ]

    @staticmethod
    def ensure_default_tools_registered() -> None:
        try:
            from src.autobots.llm.tools.tools_map import TOOLS_MAP, ToolRegistry

            if "gemini_context_tool" not in TOOLS_MAP:
                from src.autobots.llm.tools.action_context_tool_gemini import (
                    ActionContextToolGemini,
                )

                ToolRegistry.register("gemini_context_tool", ActionContextToolGemini)

            if "gemini_code_execution_tool" not in TOOLS_MAP:
                from src.autobots.llm.tools.action_code_execution_gemini import (
                    ActionCodeExecutionGemini,
                )

                ToolRegistry.register("gemini_code_execution_tool", ActionCodeExecutionGemini)

            if "gemini_chat_memory_tool" not in TOOLS_MAP:
                from src.autobots.conn.gemini.chat_memory_tool import (
                    gemini_chat_memory_tool,
                )

                ToolRegistry.register("gemini_chat_memory_tool", gemini_chat_memory_tool)
        except Exception as exc:
            logger.exception("Failed to register default Gemini tools: {}", exc)
            raise
    CHAT_MEMORY_TOOL_SYSTEM_INSTRUCTION = """Chat Memory Tool Policy

You have access to `gemini_chat_memory_tool`, a compact chat-scoped memory tool for storing and retrieving high-value information during this conversation.

Purpose
- Use this memory tool to preserve important context that is likely to help in later turns.
- Treat it as compact working memory, not as a transcript store.

What to store
Store only concise, high-signal items such as:
- `goal`: the current user objective or active sub-goal
- `decision`: an agreed design or implementation decision
- `fact`: an important confirmed fact discovered during the chat
- `preference`: a stable user preference or workflow preference
- `constraint`: an important limitation, rule, or requirement
- `open_question`: an unresolved question that may matter later
- `note`: a small important reminder that does not fit the above kinds

What not to store
Do NOT store:
- raw logs
- large tool outputs
- raw API responses
- long file contents
- giant JSON blobs
- temporary reasoning traces
- repetitive conversational filler
- duplicate entries for the same idea

When to write memory
Use `put` only when information is likely to matter in later turns.
Good times to store memory include:
- the user clarifies a goal
- a decision is agreed
- a stable fact is confirmed
- a preference is expressed
- a relevant constraint is introduced
- an important open question appears

When to read memory
Use `get` or `list` when:
- the user refers to earlier decisions or preferences
- continuity matters across many turns
- you need to confirm the current goal, constraints, or prior decisions
- you suspect an important earlier fact was stored

How to write memory well
- Use short, stable keys
- Keep values concise and specific
- Prefer updating an existing key over creating duplicates
- Do not store information unless it is genuinely useful later

Behavior guidance
- Use it selectively and intentionally
- Infer wisely while chatting with user , if some information needs to be stored than store it in your memory.
- Prefer storing durable context, not transient execution details
- If a fact, decision, constraint, or preference is likely to influence later actions, memory is appropriate"""

    FILE_REFRESH_BUFFER_HOURS = 47

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)


    @staticmethod
    def _ensure_action_run_scoped_chat_id(*, ctx: Context, config: GeminiChatConfig) -> None:
        run_id = getattr(ctx, "consumer_id", None)
        if run_id:
            config.chat_id = str(run_id)
            return
        if not getattr(config, "chat_id", None):
            config.chat_id = getattr(ctx, "trace_id", None) or str(gen_uuid())

    @classmethod
    def _datetime_to_iso(cls, value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @classmethod
    def _parse_datetime(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        raw = str(value).strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _manifest_timing_fields(cls, *, file_obj: Any | None = None, now: datetime | None = None) -> dict[str, str | None]:
        checked_at = now or cls._utc_now()
        expiration_dt = cls._parse_datetime(getattr(file_obj, "expiration_time", None)) if file_obj is not None else None
        refresh_after_dt = checked_at + timedelta(hours=cls.FILE_REFRESH_BUFFER_HOURS)
        if expiration_dt is not None:
            refresh_after_dt = min(refresh_after_dt, expiration_dt - timedelta(hours=1))
            if refresh_after_dt < checked_at:
                refresh_after_dt = checked_at
        return {
            "uploaded_at": cls._datetime_to_iso(checked_at),
            "refresh_after_at": cls._datetime_to_iso(refresh_after_dt),
            "last_checked_at": cls._datetime_to_iso(checked_at),
            "gemini_expiration_time": cls._datetime_to_iso(expiration_dt),
        }

    @classmethod
    def _manifest_entry_needs_refresh(cls, item: dict[str, Any], *, now: datetime | None = None) -> bool:
        if not item.get("source_url") or not item.get("gemini_file_name"):
            return True
        if is_generic_binary_mime_type(item.get("mime_type")) or not item.get("mime_type"):
            return True
        refresh_after_dt = cls._parse_datetime(item.get("refresh_after_at"))
        if refresh_after_dt is None:
            return True
        effective_now = now or cls._utc_now()
        return refresh_after_dt <= effective_now

    def __init__(
        self,
        *,
        user: UserORM,
        db: AsyncIOMotorDatabase | None,
        gemini_client: Any,
    ) -> None:
        self.user = user
        self.db = db
        self.gemini_client = gemini_client
        self._last_config: GeminiChatConfig | None = None

        self.ensure_default_tools_registered()

    @staticmethod
    def _to_plain(value: Any) -> Any:
        return GeminiMessageNormalizer.to_plain(value)

    @staticmethod
    def _normalize_message(message: Any) -> dict[str, Any]:
        return GeminiMessageNormalizer.normalize_message(message)

    @staticmethod
    def _current_turn(config: GeminiChatConfig) -> int:
        return max(1, int(getattr(config, "chat_turn", 1) or 1))

    @staticmethod
    def _visible_turns(config: GeminiChatConfig) -> int:
        raw = getattr(config, "visible_turns", 20)
        try:
            value = int(raw)
        except Exception:
            value = 20
        return max(1, min(200, value))

    @staticmethod
    def _dedupe_preserve_order(items: list[str] | None) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for item in items or []:
            if not item:
                continue
            item = str(item)
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return deduped

    @staticmethod
    def _display_name_from_url(url: str) -> str:
        path = urlparse(url).path or ""
        name = path.rsplit("/", 1)[-1]
        return name or "generated_file"

    @staticmethod
    def _notify_stream_text(*, ctx: Context, notify: Notify | None, text: str) -> None:
        if not notify or not text:
            return
        try:
            notify(ctx, text)
        except Exception as exc:
            logger.bind(ctx=ctx).debug("Failed forwarding Gemini stream text chunk: {}", exc)


    @staticmethod
    def _notify_tool_args(*, ctx: Context, notify: Notify | None, name: str, args: Any) -> None:
        if not notify:
            return
        try:
            streamer = ToolArgsStreamer(name, key_char_limit=100, value_char_limit=100)
            if args:
                streamer.finalize_from_full(json.dumps(args, ensure_ascii=False, default=str))
            notify(ctx, streamer.render())
        except Exception as exc:
            logger.bind(ctx=ctx).debug("Failed sending Gemini tool args notify: {}", exc)

    @staticmethod
    def _notify_status(*, ctx: Context, notify: Notify | None, message: str) -> None:
        if not notify:
            return
        try:
            notify(ctx, f"{message}")
        except Exception as exc:
            logger.bind(ctx=ctx).debug("Failed sending Gemini status notify: {}", exc)

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
            logger.bind(ctx=ctx).debug("Failed sending Gemini tool notify: {}", exc)

    @classmethod
    def ensure_pinned_messages_initialized(cls, config: GeminiChatConfig) -> None:
        if cls._current_turn(config) != 1:
            return
        if config.pinned_messages:
            return
        config.pinned_messages = cls._to_plain(config.messages or [])


    @staticmethod
    def _part_is_tool_plumbing(part: dict[str, Any]) -> bool:
        return bool(
            part.get("function_call")
            or part.get("functionCall")
            or part.get("function_response")
            or part.get("functionResponse")
        )

    @classmethod
    def _sanitize_message_for_replay(
        cls,
        message: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        raw_message = cls._to_plain(message)
        if not isinstance(raw_message, dict):
            return None

        sanitized_parts: list[dict[str, Any]] = []
        for raw_part in raw_message.get("parts") or []:
            if not isinstance(raw_part, dict):
                continue
            if cls._part_is_tool_plumbing(raw_part):
                continue
            sanitized_parts.append(raw_part)

        if not sanitized_parts:
            return None

        sanitized_message = dict(raw_message)
        sanitized_message["parts"] = sanitized_parts
        return sanitized_message

    @classmethod
    def _sanitize_messages_for_replay(
        cls,
        messages: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        sanitized: list[dict[str, Any]] = []
        for raw_message in cls._to_plain(messages or []):
            if not isinstance(raw_message, dict):
                continue
            cleaned = cls._sanitize_message_for_replay(raw_message)
            if cleaned is not None:
                sanitized.append(cleaned)
        return sanitized

    @classmethod
    def rebuild_working_messages_from_history(cls, config: GeminiChatConfig) -> None:
        pinned = cls._sanitize_messages_for_replay(config.pinned_messages or [])
        history = cls._to_plain(config.turn_history or [])
        visible = cls._visible_turns(config)

        window = history[-visible:] if history else []
        flattened: list[dict[str, Any]] = []
        for raw_turn in window:
            if not isinstance(raw_turn, dict):
                continue
            cleaned_messages = cls._sanitize_messages_for_replay(raw_turn.get("messages") or [])
            flattened.extend(cleaned_messages)
        config.messages = [*pinned, *flattened]

    @classmethod
    def append_current_turn_to_history(
        cls,
        *,
        config: GeminiChatConfig,
        turn: int,
        start_index: int,
        tool_results: list[dict[str, Any]] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> bool:
        if start_index < 0:
            return False
        all_msgs = list(config.messages or [])
        delta = cls._to_plain(all_msgs[start_index:])
        if not delta:
            return False

        record = GeminiTurnRecord(
            turn=int(turn),
            messages=delta,
            tool_results=tool_results or [],
            usage=usage,
        )
        history = cls._to_plain(config.turn_history or [])
        history.append(record.model_dump(exclude_none=True))
        config.turn_history = history
        return True

    @staticmethod
    def minimize_persisted_messages(config: GeminiChatConfig) -> None:
        config.messages = []

    @classmethod
    def default_tool_names(
        cls,
        *,
        config: GeminiChatConfig,
        action_input: DataBlock | None,
    ) -> list[str]:
        names: list[str] = []
        for default in cls.DEFAULT_TOOL_NAMES:
            if default not in names:
                names.append(default)
        for raw in list(config.default_tool_names()) + list(getattr(config, "tools", []) or []):
            if isinstance(raw, str) and raw not in names:
                names.append(raw)
        for raw in list(action_input.tools or []) if action_input else []:
            if isinstance(raw, str) and raw not in names:
                names.append(raw)
        return names

    @classmethod
    def _collect_file_uris_from_messages(
        cls,
        messages: list[dict[str, Any]] | None,
    ) -> set[str]:
        uris: set[str] = set()
        for message in cls._to_plain(messages or []):
            if not isinstance(message, dict):
                continue
            for part in message.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                file_data = part.get("file_data") or part.get("fileData") or {}
                if not isinstance(file_data, dict):
                    continue
                file_uri = file_data.get("file_uri") or file_data.get("fileUri")
                if file_uri:
                    uris.add(str(file_uri))
        return uris

    @classmethod
    def _rewrite_file_parts_in_messages(
        cls,
        messages: list[dict[str, Any]] | None,
        replacements_by_uri: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        rewritten = cls._to_plain(messages or [])
        if not replacements_by_uri:
            return rewritten

        for message in rewritten:
            if not isinstance(message, dict):
                continue
            for part in message.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                key = "file_data" if isinstance(part.get("file_data"), dict) else "fileData"
                file_data = part.get(key) or {}
                if not isinstance(file_data, dict):
                    continue
                current_uri = file_data.get("file_uri") or file_data.get("fileUri")
                replacement = replacements_by_uri.get(str(current_uri or ""))
                if not replacement:
                    continue
                if key == "file_data":
                    file_data["file_uri"] = replacement.get("file_uri")
                    file_data["mime_type"] = replacement.get("mime_type")
                else:
                    file_data["fileUri"] = replacement.get("file_uri")
                    file_data["mimeType"] = replacement.get("mime_type")
        return rewritten


    @classmethod
    def _strip_file_data_parts_in_messages(
        cls,
        messages: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """Remove Gemini file attachments from messages.

        We avoid file_data/fileData parts because they can massively bloat the effective input
        size (and have caused max input token errors in practice). We keep text parts only.
        """
        out: list[dict[str, Any]] = []
        for message in cls._to_plain(messages or []):
            if not isinstance(message, dict):
                continue
            parts_out: list[Any] = []
            for part in message.get("parts") or []:
                if not isinstance(part, dict):
                    parts_out.append(part)
                    continue
                if isinstance(part.get("file_data"), dict) or isinstance(part.get("fileData"), dict):
                    continue
                parts_out.append(part)
            if not parts_out:
                # Drop messages that only contained file attachments.
                continue
            new_msg = dict(message)
            new_msg["parts"] = parts_out
            out.append(new_msg)
        return out
    @classmethod
    def _rewrite_file_parts_in_history(
        cls,
        turn_history: list[dict[str, Any]] | None,
        replacements_by_uri: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        rewritten_history = cls._to_plain(turn_history or [])
        if not replacements_by_uri:
            return rewritten_history

        for turn in rewritten_history:
            if not isinstance(turn, dict):
                continue
            turn["messages"] = cls._rewrite_file_parts_in_messages(
                turn.get("messages") or [],
                replacements_by_uri,
            )
        return rewritten_history

    async def _preflight_refresh_uploaded_files(
        self,
        *,
        ctx: Context,
        config: GeminiChatConfig,
        current_turn_urls: list[str] | None = None,
    ) -> None:
        manifest = self._to_plain(config.uploaded_files_manifest or [])
        if not manifest:
            return

        now = self._utc_now()
        referenced_uris = self._collect_file_uris_from_messages(config.messages or [])
        current_urls = {str(url) for url in current_turn_urls or [] if url}

        relevant_items: list[dict[str, Any]] = []
        for item in manifest:
            if not isinstance(item, dict):
                continue
            source_url = str(item.get("source_url") or "")
            file_uri = str(item.get("file_uri") or "")
            if source_url in current_urls or (file_uri and file_uri in referenced_uris):
                relevant_items.append(item)

        if not relevant_items:
            return

        due_items = [
            item
            for item in relevant_items
            if self._manifest_entry_needs_refresh(item, now=now)
        ]
        if not due_items:
            return

        upload_urls = self._dedupe_preserve_order(
            [str(item.get("source_url") or "") for item in due_items if item.get("source_url")]
        )
        if not upload_urls:
            return

        # Lazy import: FileUploader pulls in optional heavy deps (e.g., pandas via file_converter).
        from src.autobots.files.file_uploader import FileUploader
        refreshed = await FileUploader.upload_urls_to_gemini(
            ctx=ctx,
            user=self.user,
            db=self.db,
            urls=upload_urls,
            gemini_client=self.gemini_client,
        )

        replacements_by_uri: dict[str, dict[str, Any]] = {}
        for item in due_items:
            source_url = str(item.get("source_url") or "")
            refreshed_file = refreshed.get(source_url)
            if refreshed_file is None:
                raise HTTPException(
                    status_code=503,
                    detail=f"Failed to refresh Gemini file upload for source URL: {source_url}",
                )

            old_file_uri = str(item.get("file_uri") or "") or None
            file_uri = getattr(refreshed_file, "uri", None)
            gemini_name = getattr(refreshed_file, "name", None)
            file_name = getattr(refreshed_file, "display_name", None) or item.get("file_name") or self._display_name_from_url(source_url)
            mime_type = resolve_file_mime_type(
                direct_candidates=[getattr(refreshed_file, "mime_type", None), item.get("mime_type")],
                file_name=file_name,
                source_url=source_url,
            )
            if not file_uri or not gemini_name or not mime_type or is_generic_binary_mime_type(mime_type):
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to refresh Gemini file metadata for source URL: {source_url}",
                )

            item["gemini_file_name"] = str(gemini_name)
            item["file_uri"] = str(file_uri)
            item["file_name"] = str(file_name)
            item["mime_type"] = str(mime_type)
            item["last_used_turn"] = self._current_turn(config)
            item.update(self._manifest_timing_fields(file_obj=refreshed_file, now=now))

            if old_file_uri:
                replacements_by_uri[old_file_uri] = {
                    "file_uri": str(file_uri),
                    "mime_type": str(mime_type),
                }

        config.uploaded_files_manifest = manifest
        if replacements_by_uri:
            config.pinned_messages = self._rewrite_file_parts_in_messages(
                config.pinned_messages,
                replacements_by_uri,
            )
            config.turn_history = self._rewrite_file_parts_in_history(
                config.turn_history,
                replacements_by_uri,
            )
            config.messages = self._rewrite_file_parts_in_messages(
                config.messages,
                replacements_by_uri,
            )
            
    async def resolve_tool_definitions(
        self,
        *,
        config: GeminiChatConfig,
        action_input: DataBlock | None,
    ) -> tuple[list[str], list[Any]]:
        tool_names = self.default_tool_names(config=config, action_input=action_input)
        tool_defs = await ToolFactoryGemini.get_tool_definations(tool_names)
        return tool_names, list(tool_defs or [])
    
    @classmethod
    def _upsert_uploaded_file_manifest(
        cls,
        config: GeminiChatConfig,
        *,
        source_url: str,
        gemini_file_name: str,
        file_uri: str | None,
        file_name: str | None,
        mime_type: str | None,
        source: str = "current_turn",
        uploaded_at: str | None = None,
        refresh_after_at: str | None = None,
        last_checked_at: str | None = None,
        gemini_expiration_time: str | None = None,
    ) -> None:
        turn = cls._current_turn(config)
        manifest = cls._to_plain(config.uploaded_files_manifest or [])
        updated = False
        for item in manifest:
            if not isinstance(item, dict):
                continue
            if str(item.get("source_url") or "") != str(source_url):
                continue
            item["gemini_file_name"] = gemini_file_name
            item["file_uri"] = file_uri
            item["file_name"] = file_name
            item["mime_type"] = mime_type
            item["last_used_turn"] = turn
            if uploaded_at is not None:
                item["uploaded_at"] = uploaded_at
            if refresh_after_at is not None:
                item["refresh_after_at"] = refresh_after_at
            if last_checked_at is not None:
                item["last_checked_at"] = last_checked_at
            if gemini_expiration_time is not None:
                item["gemini_expiration_time"] = gemini_expiration_time
            updated = True
            break

        if not updated:
            manifest.append(
                GeminiUploadedFileRef(
                    source_url=str(source_url),
                    gemini_file_name=str(gemini_file_name),
                    file_uri=file_uri,
                    file_name=file_name,
                    mime_type=mime_type,
                    uploaded_at=uploaded_at,
                    refresh_after_at=refresh_after_at,
                    last_checked_at=last_checked_at,
                    gemini_expiration_time=gemini_expiration_time,
                    first_uploaded_turn=turn,
                    last_used_turn=turn,
                    source=source,  # type: ignore[arg-type]
                ).model_dump(exclude_none=True)
            )
        config.uploaded_files_manifest = manifest

    @classmethod
    def _find_uploaded_file_entry(
        cls,
        config: GeminiChatConfig,
        source_url: str,
    ) -> dict[str, Any] | None:
        for item in cls._to_plain(config.uploaded_files_manifest or []):
            if isinstance(item, dict) and str(item.get("source_url") or "") == str(source_url):
                return item
        return None

    async def _file_parts_from_urls(
        self,
        *,
        ctx: Context,
        config: GeminiChatConfig,
        urls: list[str],
    ) -> list[Any]:
        if not urls:
            return []

        urls = self._dedupe_preserve_order(urls)
        parts: list[Any] = []
        to_upload: list[str] = []

        for url in urls:
            existing = self._find_uploaded_file_entry(config, url)
            if not existing:
                to_upload.append(url)
                continue
            try:
                file_obj = await self.gemini_client.get_file(
                    ctx=ctx,
                    gemini_filename=str(existing.get("gemini_file_name")),
                )
                file_uri = getattr(file_obj, "uri", None)
                gemini_name = getattr(file_obj, "name", None) or existing.get("gemini_file_name")
                file_name = (
                    getattr(file_obj, "display_name", None)
                    or existing.get("file_name")
                    or self._display_name_from_url(url)
                )
                mime_type = resolve_file_mime_type(
                    direct_candidates=[
                        getattr(file_obj, "mime_type", None),
                        existing.get("mime_type"),
                    ],
                    file_name=file_name,
                    source_url=url,
                )
                if (
                    not file_uri
                    or not gemini_name
                    or not mime_type
                    or is_generic_binary_mime_type(mime_type)
                ):
                    to_upload.append(url)
                    continue
                parts.append(
                    GeminiMessageNormalizer.normalize_part(
                        types.Part(
                            file_data=types.FileData(
                                file_uri=file_uri,
                                mime_type=mime_type,
                            )
                        )
                    )
                )
                self._upsert_uploaded_file_manifest(
                    config,
                    source_url=url,
                    gemini_file_name=str(gemini_name),
                    file_uri=file_uri,
                    file_name=file_name,
                    mime_type=mime_type,
                    last_checked_at=self._manifest_timing_fields(file_obj=file_obj)["last_checked_at"],
                )
            except Exception:
                to_upload.append(url)

        if to_upload:
            # Lazy import: FileUploader pulls in optional heavy deps (e.g., pandas via file_converter).
            from src.autobots.files.file_uploader import FileUploader
            uploaded = await FileUploader.upload_urls_to_gemini(
                ctx=ctx,
                user=self.user,
                db=self.db,
                urls=to_upload,
                gemini_client=self.gemini_client,
            )
            for url, gemini_file in uploaded.items():
                file_uri = getattr(gemini_file, "uri", None)
                gemini_name = getattr(gemini_file, "name", None)
                file_name = getattr(gemini_file, "display_name", None) or self._display_name_from_url(url)
                mime_type = resolve_file_mime_type(
                    direct_candidates=[getattr(gemini_file, "mime_type", None)],
                    file_name=file_name,
                    source_url=url,
                )
                if not file_uri or not gemini_name:
                    continue
                if not mime_type or is_generic_binary_mime_type(mime_type):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Could not determine a supported MIME type for uploaded file URL: {url}. "
                            "Gemini requires a concrete MIME type such as application/pdf, image/png, "
                            "audio/mpeg, or video/mp4."
                        ),
                    )
                parts.append(
                    GeminiMessageNormalizer.normalize_part(
                        types.Part(
                            file_data=types.FileData(
                                file_uri=file_uri,
                                mime_type=mime_type,
                            )
                        )
                    )
                )
                manifest_timing = self._manifest_timing_fields(file_obj=gemini_file)
                self._upsert_uploaded_file_manifest(
                    config,
                    source_url=url,
                    gemini_file_name=str(gemini_name),
                    file_uri=file_uri,
                    file_name=file_name,
                    mime_type=mime_type,
                    uploaded_at=manifest_timing["uploaded_at"],
                    refresh_after_at=manifest_timing["refresh_after_at"],
                    last_checked_at=manifest_timing["last_checked_at"],
                    gemini_expiration_time=manifest_timing["gemini_expiration_time"],
                )

        return parts

    @classmethod
    def _extract_generated_file_refs_from_tool_output(
        cls,
        *,
        tool_name: str,
        tool_text: str,
        fallback_urls: list[str],
        turn: int,
    ) -> list[dict[str, Any]]:
        generated: list[dict[str, Any]] = []
        payload = None
        try:
            payload = json.loads(tool_text) if tool_text else None
        except Exception:
            payload = None

        raw_items = []
        if isinstance(payload, dict):
            raw_items = payload.get("generated_files") or []
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                url = item.get("url") or item.get("s3_url")
                if not url:
                    continue
                generated.append(
                    GeminiGeneratedFileRef(
                        url=str(url),
                        file_name=str(item.get("file_name") or item.get("filename") or cls._display_name_from_url(str(url))),
                        mime_type=item.get("mime_type"),
                        file_id=item.get("file_id"),
                        source="code_execution" if tool_name == "gemini_code_execution_tool" else "tool_output",
                        created_turn=turn,
                        last_seen_turn=turn,
                    ).model_dump(exclude_none=True)
                )

        existing_urls = {str(item.get('url')) for item in generated if isinstance(item, dict)}
        for url in fallback_urls:
            if str(url) in existing_urls:
                continue
            generated.append(
                GeminiGeneratedFileRef(
                    url=str(url),
                    file_name=cls._display_name_from_url(str(url)),
                    source="code_execution" if tool_name == "gemini_code_execution_tool" else "tool_output",
                    created_turn=turn,
                    last_seen_turn=turn,
                ).model_dump(exclude_none=True)
            )
        return generated

    @classmethod
    def _merge_generated_files_history(
        cls,
        previous_files: list[Any] | None,
        current_turn_files: list[Any] | None,
    ) -> list[dict[str, Any]]:
        merged_by_identity: dict[str, dict[str, Any]] = {}
        order: list[str] = []

        for raw_item in (previous_files or []) + (current_turn_files or []):
            item = cls._to_plain(raw_item)
            if not isinstance(item, dict):
                continue
            identity = str(item.get("file_id") or item.get("url") or item.get("file_name") or "")
            if not identity:
                continue
            if identity not in merged_by_identity:
                merged_by_identity[identity] = item
                order.append(identity)
                continue
            existing = merged_by_identity[identity]
            for key, value in item.items():
                if value is None:
                    continue
                if key == "created_turn":
                    try:
                        existing[key] = min(int(existing.get(key) or value), int(value))
                    except Exception:
                        existing[key] = existing.get(key) or value
                    continue
                if key == "last_seen_turn":
                    try:
                        existing[key] = max(int(existing.get(key) or value), int(value))
                    except Exception:
                        existing[key] = value
                    continue
                existing[key] = value
        return [merged_by_identity[item_id] for item_id in order]

    async def build_user_message(
        self,
        *,
        ctx: Context,
        config: GeminiChatConfig,
        action_input: DataBlock,
    ) -> dict[str, Any]:
        current_turn_urls = self._dedupe_preserve_order(list(action_input.urls or []))
        config.current_turn_urls = current_turn_urls
        config.all_turn_urls = self._dedupe_preserve_order(
            list(config.all_turn_urls or []) + current_turn_urls
        )

        parts: list[dict[str, Any]] = []

        enabled_tool_names = self.default_tool_names(config=config, action_input=action_input)
        file_search_store_tool_enabled = "gemini_file_search_store_tool" in enabled_tool_names

        if file_search_store_tool_enabled:
            await self._auto_index_current_turn_urls_in_file_search_store(ctx=ctx, config=config)
            block = build_file_search_store_context_block(config=config)
            if block is not None:
                parts.append(block)

        prompt = action_input.get_llm_prompt(ctx=ctx)
        parts.append({"text": prompt})
        return {"role": "user", "parts": parts}

    @classmethod
    def _tool_defs_include_chat_memory_tool(cls, tool_defs: list[Any]) -> bool:
        plain = GeminiMessageNormalizer.to_plain(tool_defs)
        for tool in plain or []:
            if not isinstance(tool, dict):
                continue
            declarations = tool.get("function_declarations") or tool.get("functionDeclarations") or []
            for declaration in declarations:
                if not isinstance(declaration, dict):
                    continue
                if str(declaration.get("name") or "") == "gemini_chat_memory_tool":
                    return True
        return False

    @classmethod
    def _resolved_system_instruction(
        cls,
        *,
        config: GeminiChatConfig,
        tool_defs: list[Any],
    ) -> str | list[dict[str, Any]] | None:
        base = config.system_instruction
        if not cls._tool_defs_include_chat_memory_tool(tool_defs):
            return base
        policy = cls.CHAT_MEMORY_TOOL_SYSTEM_INSTRUCTION
        if base is None:
            return policy
        if isinstance(base, str):
            if policy in base:
                return base
            return f"{base.strip()}\n\n{policy}" if base.strip() else policy
        if isinstance(base, list):
            plain = GeminiMessageNormalizer.to_plain(base)
            policy_part = {"text": policy}
            if any(isinstance(item, dict) and item.get("text") == policy for item in plain):
                return plain
            return [*plain, policy_part]
        return base

    def _run_chat_memory_tool(
        self,
        *,
        args: dict[str, Any],
        current_turn: int,
    ) -> DataBlock:
        req = GeminiChatMemoryToolReq.model_validate(args)
        result, updated_memory = GeminiChatMemoryToolService.execute(
            req,
            chat_memory=self._last_config.chat_memory if self._last_config else [],
            current_turn=current_turn,
        )
        if self._last_config is not None:
            self._last_config.chat_memory = updated_memory
        return DataBlock(text=result.model_dump_json(exclude_none=True))

    async def _run_context_tool_semantic_search(
        self,
        *,
        ctx: Context,
        notify: Notify | None,
        semantic_query: str,
        semantic_max_results: int,
    ) -> DataBlock:
        history_vector_store_id = getattr(self._last_config, "history_vector_store_id", None)

        if not history_vector_store_id:
            payload = {
                "ok": True,
                "operation": "semantic_query",
                "total_count": 0,
                "returned_count": 0,
                "matches": [],
                "meta": {
                    "note": (
                        "No conversation-memory vector store exists yet "
                        "(nothing indexed so far). Fall back to get_turns."
                    ),
                },
            }
            return DataBlock(text=json.dumps(payload, ensure_ascii=False))

        vs_result = await openai_vector_store_tool(
            config=OpenAIVectorStoreToolConfig(
                operation=OpenAIVectorStoreToolOperation.QUERY_VECTOR_STORE,
                vector_store_id=history_vector_store_id,
                query=semantic_query,
                max_num_results=max(1, min(20, semantic_max_results)),
            ),
            ctx=ctx,
            user=self.user,
            db=self.db,
            notify=notify,
        )
        if isinstance(vs_result, Exception):
            payload = {
                "ok": False,
                "operation": "semantic_query",
                "total_count": 0,
                "returned_count": 0,
                "matches": [],
                "meta": {"error": str(vs_result)},
            }
            return DataBlock(text=json.dumps(payload, ensure_ascii=False))

        vs_payload: dict[str, Any] = {}
        if vs_result.blocks and vs_result.blocks[0].text:
            vs_payload = json.loads(vs_result.blocks[0].text)

        vs_results = vs_payload.get("results") or []
        payload = {
            "ok": True,
            "operation": "semantic_query",
            "total_count": len(vs_results),
            "returned_count": len(vs_results),
            "matches": vs_results,
            "meta": {
                "mode": "semantic",
                "semantic_query": semantic_query,
                "history_vector_store_id": history_vector_store_id,
            },
        }
        return DataBlock(text=json.dumps(payload, ensure_ascii=False))

    @classmethod
    def build_generation_config(
        cls,
        *,
        config: GeminiChatConfig,
        tool_defs: list[Any],
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if tool_defs:
            kwargs["tools"] = tool_defs
            tool_kwargs: dict[str, Any] = {"mode": config.function_calling_mode()}
            allowed = config.allowed_function_names()
            if allowed:
                tool_kwargs["allowed_function_names"] = allowed
            kwargs["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(**tool_kwargs)
            )
        if config.thinking is not None:
            thinking_plain = GeminiMessageNormalizer.to_plain(config.thinking)
            if isinstance(thinking_plain, dict):
                kwargs["thinking_config"] = types.ThinkingConfig(**thinking_plain)
        system_instruction = cls._resolved_system_instruction(config=config, tool_defs=tool_defs)
        if system_instruction is not None:
            kwargs["system_instruction"] = system_instruction
        if config.max_output_tokens is not None:
            kwargs["max_output_tokens"] = config.max_output_tokens
        if config.temperature is not None:
            kwargs["temperature"] = config.temperature
        if config.top_p is not None:
            kwargs["top_p"] = config.top_p
        if config.top_k is not None:
            kwargs["top_k"] = config.top_k
        return types.GenerateContentConfig(**kwargs)

    async def run_tool_calls(
        self,
        *,
        ctx: Context,
        app_state: AppState,
        function_calls: list[dict[str, Any]],
        notify: Notify | None,
        output_text_to_file_char_limit: int | None,
        step_index: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        response_parts: list[dict[str, Any]] = []
        persisted_results: list[dict[str, Any]] = []
        current_turn_generated_files: list[dict[str, Any]] = []
        tool_factory = ToolFactoryGemini(user=self.user, db=self.db)
        current_turn = self._current_turn(self._last_config)

        for call in function_calls:
            app_state.raise_if_cancelled()
            name = str(call.get("name") or "")
            args = call.get("args") or {}
            if not isinstance(args, dict):
                args = {"value": args}
            call_id = str(call.get("id") or f"gemini-tool-{gen_uuid()}")

            runtime_args = dict(args)
            if name == "gemini_context_tool":
                runtime_args = {
                    **runtime_args,
                    "turn_history": GeminiMessageNormalizer.to_plain(self._last_config.turn_history or []),
                    "tool_results": GeminiMessageNormalizer.to_plain(self._last_config.tool_results_history or []),
                    "pinned_messages": GeminiMessageNormalizer.to_plain(self._last_config.pinned_messages or []),
                }

            elif name == "gemini_file_search_store_tool":
                if not runtime_args.get("store_ref"):
                    # Prefer the per-chat file-search store for this run (prevents cross-chat contamination).
                    if getattr(self._last_config, "file_search_store_ref", None):
                        runtime_args["store_ref"] = GeminiMessageNormalizer.to_plain(
                            self._last_config.file_search_store_ref
                        )
                    else:
                        op = str(runtime_args.get("operation") or "")
                        if op in {
                            "get_store",
                            "delete_store",
                            "query_store",
                            "stats",
                            "delete_documents",
                            "reindex_documents",
                            "ingest_files",
                        }:
                            ensured = await self._ensure_file_search_store(ctx=ctx, config=self._last_config)
                            if ensured:
                                runtime_args["store_ref"] = GeminiMessageNormalizer.to_plain(ensured)

            self._notify_tool_args(ctx=ctx, notify=notify, name=name, args=args)
            semantic_query = str(args.get("semantic_query") or "").strip() if name == "gemini_context_tool" else ""

            if name == "gemini_chat_memory_tool":
                tool_output = self._run_chat_memory_tool(
                    args=runtime_args,
                    current_turn=current_turn,
                )
            elif name == "gemini_context_tool" and semantic_query:
                tool_output = await self._run_context_tool_semantic_search(
                    ctx=ctx,
                    notify=notify,
                    semantic_query=semantic_query,
                    semantic_max_results=int(args.get("semantic_max_results") or 5),
                )
            else:
                tool_output = await tool_factory.run_tool(
                    ctx=ctx,
                    app_state=app_state,
                    tool_name=name,
                    tool_args=runtime_args,
                    notify=notify,
                    output_text_to_file_char_limit=output_text_to_file_char_limit,
                )

            tool_text = str(tool_output.text or "")
            is_error = tool_text.strip().lower().startswith("error")

            # Keep GeminiChatConfig state updated live after tool calls (Claude-style).
            if name == "gemini_file_search_store_tool" and not is_error:
                self._apply_file_search_store_tool_state_update(
                    config=self._last_config,
                    tool_text=tool_text,
                    current_turn=current_turn,
                )

            generated_files = self._extract_generated_file_refs_from_tool_output(
                tool_name=name,
                tool_text=tool_text,
                fallback_urls=list(tool_output.urls or []),
                turn=current_turn,
            )
            current_turn_generated_files.extend(generated_files)
            response_payload = {
                "ok": not is_error,
                "text": tool_text,
                "urls": list(tool_output.urls or []),
                "file_ids": list(tool_output.file_ids or []),
                "generated_files": generated_files,
            }
            self._notify_tool_event(
                ctx=ctx,
                notify=notify,
                kind="result" if not is_error else "error",
                name=name,
                payload={
                    "id": call_id,
                    "is_error": is_error,
                    "urls": list(tool_output.urls or []),
                    "generated_files_count": len(generated_files),
                },
            )
            part = types.Part(
                function_response=types.FunctionResponse(
                    id=call_id,
                    name=name,
                    response=response_payload,
                )
            )
            response_parts.append(GeminiMessageNormalizer.normalize_part(part))
            persisted_results.append(
                GeminiStoredToolResult(
                    tool_use_id=call_id,
                    name=name,
                    source=(
                        "context"
                        if name == "gemini_context_tool"
                        else "file_search_store"
                        if name == "gemini_file_search_store_tool"
                        else "code_execution"
                        if name == "gemini_code_execution_tool"
                        else "custom"
                    ),
                    chat_turn=current_turn,
                    step_index=step_index,
                    input=args if isinstance(args, dict) else {"value": args},
                    output_text=tool_text,
                    is_error=is_error,
                    urls=list(tool_output.urls or []),
                    generated_files=generated_files,
                    meta={"tool_name": name},
                ).model_dump(exclude_none=True)
            )
        return response_parts, persisted_results, current_turn_generated_files

    @staticmethod
    def extract_usage_metadata(response: Any, *, turn: int) -> dict[str, Any]:
        plain = GeminiMessageNormalizer.normalize_response(response)
        usage = plain.get("usage_metadata") or plain.get("usageMetadata") or {}
        finish_reason = GeminiMessageNormalizer.first_finish_reason(plain)
        if not isinstance(usage, dict):
            usage = {}
        usage_record = GeminiUsageMetadataRecord(
            chat_turn=turn,
            prompt_token_count=usage.get("prompt_token_count") or usage.get("promptTokenCount"),
            candidates_token_count=usage.get("candidates_token_count") or usage.get("candidatesTokenCount"),
            total_token_count=usage.get("total_token_count") or usage.get("totalTokenCount"),
            thoughts_token_count=usage.get("thoughts_token_count") or usage.get("thoughtsTokenCount"),
            tool_use_prompt_token_count=usage.get("tool_use_prompt_token_count") or usage.get("toolUsePromptTokenCount"),
            cached_content_token_count=usage.get("cached_content_token_count") or usage.get("cachedContentTokenCount"),
            finish_reason=finish_reason,
            raw=usage,
        )
        return usage_record.model_dump(exclude_none=True)

    async def _create_or_stream_message(
        self,
        *,
        ctx: Context,
        config: GeminiChatConfig,
        contents: list[dict[str, Any]],
        generation_config: Any,
        notify: Notify | None,
    ) -> Any:
        if not bool(getattr(config, "stream", False)):
            self._notify_status(
                ctx=ctx,
                notify=notify,
                message=f"Starting Gemini request on model {config.model}",
            )
            response = await self.gemini_client.generate_content(
                model=config.model,
                contents=contents,
                config=generation_config,
            )
            self._notify_status(
                ctx=ctx,
                notify=notify,
                message="Gemini response received",
            )
            return response

        self._notify_status(
            ctx=ctx,
            notify=notify,
            message=f"Starting Gemini stream on model {config.model}",
        )
        stream = await self.gemini_client.generate_content_stream(
            model=config.model,
            contents=contents,
            config=generation_config,
        )

        accumulator = GeminiStreamAccumulator()
        announced_tool_calls: set[str] = set()
        notify_main = (
            Notify(topic=text_topic(notify.topic), payload=NotifyPayload.snapshot) if notify else None
        )

        if hasattr(stream, "__aiter__"):
            async for chunk in stream:
                accumulator.ingest(chunk)
                snapshot = accumulator.snapshot()
                self._notify_stream_text(ctx=ctx, notify=notify_main, text=snapshot.text)
                for call in snapshot.function_calls:
                    call_id = str(call.get("id") or call.get("name") or "")
                    if call_id and call_id not in announced_tool_calls:
                        announced_tool_calls.add(call_id)
                        self._notify_tool_args(
                            ctx=ctx,
                            notify=notify,
                            name=str(call.get("name") or "function_call"),
                            args=call.get("args"),
                        )
        else:
            for chunk in stream:
                accumulator.ingest(chunk)
                snapshot = accumulator.snapshot()
                self._notify_stream_text(ctx=ctx, notify=notify_main, text=snapshot.text)
                for call in snapshot.function_calls:
                    call_id = str(call.get("id") or call.get("name") or "")
                    if call_id and call_id not in announced_tool_calls:
                        announced_tool_calls.add(call_id)
                        self._notify_tool_args(
                            ctx=ctx,
                            notify=notify,
                            name=str(call.get("name") or "function_call"),
                            args=call.get("args"),
                        )

        snapshot = accumulator.snapshot()
        self._notify_stream_text(ctx=ctx, notify=notify_main, text=snapshot.text)
        self._notify_status(
            ctx=ctx,
            notify=notify,
            message="Gemini stream completed",
        )
        return {
            "candidates": [
                {
                    "content": snapshot.assistant_message or {"role": "model", "parts": []},
                    "finish_reason": snapshot.finish_reason,
                }
            ],
            "usage_metadata": snapshot.usage_metadata,
        }
    @classmethod
    def _merge_file_search_store_manifest(
        cls,
        *,
        existing: list[dict[str, Any]] | None,
        incoming: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        return merge_file_search_store_manifest(existing=existing, incoming=incoming)


    _TOOL_PART_KEYS = ("function_call", "functionCall", "function_response", "functionResponse")

    @staticmethod
    def _history_vector_store_enabled(config: GeminiChatConfig) -> bool:
        return bool(getattr(config, "history_vector_store_enabled", True))

    @staticmethod
    def _history_index_every_n_turns(config: GeminiChatConfig) -> int:
        raw = getattr(config, "history_vector_store_index_every_n_turns", 20)
        try:
            value = int(raw)
        except Exception:
            value = 20
        return max(1, min(200, value))

    async def _ensure_history_vector_store_exists(
        self, *, ctx: Context, config: GeminiChatConfig, notify: Notify | None
    ) -> None:
        if getattr(config, "history_vector_store_id", None):
            return

        name = getattr(config, "history_vector_store_name", None) or f"gemini-chat-history-{gen_uuid()}"
        result = await openai_vector_store_tool(
            config=OpenAIVectorStoreToolConfig(
                operation=OpenAIVectorStoreToolOperation.CREATE_VECTOR_STORE,
                name=name,
            ),
            ctx=ctx,
            user=self.user,
            db=self.db,
            notify=notify,
        )
        payload = self._extract_vector_store_payload(result)
        vector_store = payload.get("vector_store") or {}
        vector_store_id = payload.get("vector_store_id") or vector_store.get("id")
        if not vector_store_id:
            raise RuntimeError("Failed to create conversation history vector store")

        config.history_vector_store_id = vector_store_id
        config.history_vector_store_name = vector_store.get("name") or name

    @staticmethod
    def _extract_vector_store_payload(result: Any) -> dict[str, Any]:
        if isinstance(result, Exception):
            raise result
        if result.blocks and result.blocks[0].text:
            return json.loads(result.blocks[0].text)
        return {}

    @classmethod
    def _is_tool_part(cls, part: dict[str, Any]) -> bool:
        return any(part.get(key) for key in cls._TOOL_PART_KEYS)

    @classmethod
    def _stringify_history_part(cls, part: Any) -> str:
        part = cls._to_plain(part)
        if not isinstance(part, dict):
            return str(part) if part is not None else ""

        text = part.get("text")
        if isinstance(text, str) and text:
            return text

        function_call = part.get("function_call") or part.get("functionCall")
        if isinstance(function_call, dict):
            try:
                call_args = json.dumps(function_call.get("args") or {}, ensure_ascii=False, default=str)
            except Exception:
                call_args = str(function_call.get("args"))
            return f"[called tool {function_call.get('name')} with args {call_args[:500]}]"

        function_response = part.get("function_response") or part.get("functionResponse")
        if isinstance(function_response, dict):
            try:
                response_text = json.dumps(
                    function_response.get("response") or {}, ensure_ascii=False, default=str
                )
            except Exception:
                response_text = str(function_response.get("response"))
            return f"[tool result: {response_text[:1000]}]"

        if any(part.get(key) for key in ("inline_data", "inlineData", "file_data", "fileData")):
            return "[attachment]"

        return ""

    @classmethod
    def _stringify_history_parts(cls, parts: list[Any], *, tool_parts_only: bool) -> str:
        rendered = []
        for raw_part in parts:
            part = cls._to_plain(raw_part)
            is_tool = isinstance(part, dict) and cls._is_tool_part(part)
            if is_tool != tool_parts_only:
                continue
            text = cls._stringify_history_part(part)
            if text:
                rendered.append(text)
        return "\n".join(rendered)

    @classmethod
    def _collect_turn_messages(
        cls, *, turns: list[dict[str, Any]], tool_parts_only: bool
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        for raw_turn in turns:
            turn = cls._to_plain(raw_turn)
            if not isinstance(turn, dict):
                continue

            messages: list[dict[str, str]] = []
            for raw_msg in turn.get("messages") or []:
                msg = GeminiMessageNormalizer.normalize_message(raw_msg)
                text = cls._stringify_history_parts(
                    msg.get("parts") or [], tool_parts_only=tool_parts_only
                )
                if text.strip():
                    messages.append({"role": msg.get("role") or "user", "content": text.strip()})

            if messages:
                collected.append({"turn": turn.get("turn"), "messages": messages})

        return collected

    @classmethod
    def _render_turn_history_messages_as_json(cls, *, turns: list[dict[str, Any]]) -> str:
        collected = cls._collect_turn_messages(turns=turns, tool_parts_only=False)
        return json.dumps(collected, ensure_ascii=False, default=str)

    @classmethod
    def _render_turn_history_tool_activity_as_text(cls, *, turns: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for turn in cls._collect_turn_messages(turns=turns, tool_parts_only=True):
            lines.append(f"=== Turn {turn['turn']} ===")
            lines.extend(f"[{msg['role']}] {msg['content']}" for msg in turn["messages"])
            lines.append("")
        return "\n".join(lines).strip()

    async def _upload_history_file(
        self,
        ctx: Context,
        *,
        filename: str,
        content: str,
        turn_start: int,
        turn_end: int,
        content_type: str,
    ) -> OpenAIVectorStoreFileInput:
        openai_conn = await AppConn.get_openai(ctx=ctx, db=self.db, user=self.user, provider="openai")
        if isinstance(openai_conn, Exception):
            raise openai_conn

        openai_files = OpenaiFiles(openai_client=openai_conn.client)
        file_obj = await openai_files.create(
            ctx=ctx,
            file_create=FileCreate(file=(filename, content.encode("utf-8")), purpose="assistants"),
        )
        return OpenAIVectorStoreFileInput(
            openai_file_id=file_obj.id,
            attributes={"turn_start": turn_start, "turn_end": turn_end, "content_type": content_type},
        )

    async def _replace_existing_history_vector_store_files(
        self,
        ctx: Context,
        notify: Notify | None,
        *,
        config: GeminiChatConfig,
        turn_start: int,
        turn_end: int,
        content_types: set[str],
    ) -> None:
        """Replaces a stale duplicate from a retried index; never touches other turn ranges."""
        list_result = await openai_vector_store_tool(
            config=OpenAIVectorStoreToolConfig(
                operation=OpenAIVectorStoreToolOperation.LIST_VECTOR_STORE_FILES,
                vector_store_id=config.history_vector_store_id,
            ),
            ctx=ctx,
            user=self.user,
            db=self.db,
            notify=notify,
        )
        payload = self._extract_vector_store_payload(list_result)

        stale_ids: list[str] = []
        for file_entry in payload.get("files", []):
            if not isinstance(file_entry, dict) or not file_entry.get("id"):
                continue
            attrs = file_entry.get("attributes") or {}
            same_range = attrs.get("turn_start") == turn_start and attrs.get("turn_end") == turn_end
            if same_range and attrs.get("content_type") in content_types:
                stale_ids.append(file_entry["id"])

        if not stale_ids:
            return

        await openai_vector_store_tool(
            config=OpenAIVectorStoreToolConfig(
                operation=OpenAIVectorStoreToolOperation.DELETE_VECTOR_STORE_FILES,
                vector_store_id=config.history_vector_store_id,
                delete_file_ids=stale_ids,
            ),
            ctx=ctx,
            user=self.user,
            db=self.db,
            notify=notify,
        )
        logger.bind(
            ctx=ctx, turn_start=turn_start, turn_end=turn_end, stale_file_count=len(stale_ids)
        ).info("Removed stale vector-store files before re-indexing this turn range")

    async def _index_turn_history_batch_to_vector_store(
        self,
        *,
        ctx: Context,
        config: GeminiChatConfig,
        notify: Notify | None,
        turns: list[dict[str, Any]],
        turn_start: int,
        turn_end: int,
    ) -> None:
        candidates = (
            (self._render_turn_history_messages_as_json(turns=turns), "messages", "json"),
            (self._render_turn_history_tool_activity_as_text(turns=turns), "tool_activity", "md"),
        )
        non_empty = [
            (content, content_type, ext)
            for content, content_type, ext in candidates
            if content.strip() and content.strip() != "[]"
        ]
        if not non_empty:
            return

        await self._replace_existing_history_vector_store_files(
            ctx,
            notify,
            config=config,
            turn_start=turn_start,
            turn_end=turn_end,
            content_types={content_type for _, content_type, _ in non_empty},
        )

        files_to_attach = [
            await self._upload_history_file(
                ctx,
                filename=f"conversation_{content_type}_{turn_start}_{turn_end}.{ext}",
                content=content,
                turn_start=turn_start,
                turn_end=turn_end,
                content_type=content_type,
            )
            for content, content_type, ext in non_empty
        ]

        result = await openai_vector_store_tool(
            config=OpenAIVectorStoreToolConfig(
                operation=OpenAIVectorStoreToolOperation.ADD_FILES,
                vector_store_id=config.history_vector_store_id,
                files=files_to_attach,
                wait_for_completion=True,
            ),
            ctx=ctx,
            user=self.user,
            db=self.db,
            notify=notify,
        )
        payload = self._extract_vector_store_payload(result)
        if not payload.get("ok", False):
            logger.bind(ctx=ctx, turn_start=turn_start, turn_end=turn_end).warning(
                "Conversation history vector-store indexing reported failure: {}", payload
            )

    async def _maybe_index_turn_history_to_vector_store(
        self,
        *,
        ctx: Context,
        config: GeminiChatConfig,
        notify: Notify | None,
        current_turn: int,
    ) -> None:
        """Best-effort: failures here must never break the chat turn."""
        if not self._history_vector_store_enabled(config):
            return

        every_n = self._history_index_every_n_turns(config)
        if current_turn <= 0 or current_turn % every_n != 0:
            return

        last_indexed = int(getattr(config, "history_vector_store_last_indexed_turn", 0) or 0)
        if current_turn <= last_indexed:
            return

        try:
            await self._ensure_history_vector_store_exists(ctx=ctx, config=config, notify=notify)

            history = self._to_plain(getattr(config, "turn_history", None) or [])
            batch = [
                t
                for t in history
                if isinstance(t, dict) and last_indexed < int(t.get("turn") or 0) <= current_turn
            ]
            if not batch:
                config.history_vector_store_last_indexed_turn = current_turn
                return

            await self._index_turn_history_batch_to_vector_store(
                ctx=ctx,
                config=config,
                notify=notify,
                turns=batch,
                turn_start=last_indexed + 1,
                turn_end=current_turn,
            )
            config.history_vector_store_last_indexed_turn = current_turn
            logger.bind(
                ctx=ctx,
                turn_start=last_indexed + 1,
                turn_end=current_turn,
                history_vector_store_id=getattr(config, "history_vector_store_id", None),
            ).info("Indexed conversation turn-history batch into vector store")
        except Exception as exc:
            logger.bind(ctx=ctx, current_turn=current_turn).warning(
                "Failed indexing conversation turn-history batch into vector store: {}", exc
            )

    async def _ensure_file_search_store(
        self,
        *,
        ctx: Context,
        config: GeminiChatConfig,
    ) -> dict[str, Any] | None:
        if not getattr(config, "file_search_store_enabled", True):
            return None

        # Make sure chat_id is stable for this run (action_run_id / ctx.consumer_id).
        self._ensure_action_run_scoped_chat_id(ctx=ctx, config=config)
        chat_id = getattr(config, "chat_id", None)

        existing_ref = self._to_plain(getattr(config, "file_search_store_ref", None))
        if isinstance(existing_ref, dict) and existing_ref.get("store_name"):
            # Ensure we persist chat_id on the ref for debugging/audit purposes.
            if chat_id and not existing_ref.get("chat_id"):
                existing_ref["chat_id"] = chat_id
                config.file_search_store_ref = existing_ref
            return existing_ref

        fs_client = GeminiFileSearchStoreClient(self.gemini_client)
        embedding_model = "models/gemini-embedding-2"
        display_name = f"gemini-chat-file-store-{chat_id or 'default'}"
        created = await fs_client.create_store(
            display_name=display_name,
            embedding_model=embedding_model,
        )
        created_plain = GeminiMessageNormalizer.normalize_response(created)
        store_name = created_plain.get("name")
        resolved_display_name = (
            created_plain.get("display_name")
            or created_plain.get("displayName")
            or display_name
        )

        ref = GeminiFileSearchStoreRef(
            scope="chat_scoped",
            store_name=store_name,
            display_name=resolved_display_name,
            chat_id=chat_id,
            metadata={"embedding_model": embedding_model},
        ).model_dump(exclude_none=True)
        config.file_search_store_ref = ref
        return ref

    async def _auto_index_current_turn_urls_in_file_search_store(
        self,
        *,
        ctx: Context,
        config: GeminiChatConfig,
    ) -> None:
        if not getattr(config, "file_search_store_enabled", True):
            return
        if not getattr(config, "file_search_store_auto_index_current_turn_urls", True):
            return

        current_turn = self._current_turn(config)
        if getattr(config, "file_search_store_last_indexed_turn", None) == current_turn:
            return

        urls = self._dedupe_preserve_order(list(getattr(config, "current_turn_urls", None) or []))
        if not urls:
            config.file_search_store_last_indexed_turn = current_turn
            return

        store_ref = await self._ensure_file_search_store(ctx=ctx, config=config)
        if not store_ref or not store_ref.get("store_name"):
            return

        existing_manifest = [
            item
            for item in (
                self._to_plain(getattr(config, "file_search_store_manifest", None)) or []
            )
            if isinstance(item, dict)
        ]
        existing_source_urls = {
            str(item.get("source_url") or "")
            for item in existing_manifest
            if item.get("source_url")
        }

        fs_client = GeminiFileSearchStoreClient(self.gemini_client)
        new_docs: list[dict[str, Any]] = []
        store_name = str(store_ref.get("store_name") or "").strip()
        if not store_name:
            config.file_search_store_last_indexed_turn = current_turn
            return

        max_upload_bytes = int(FILE_SEARCH_MAX_DOC_BYTES)
        sniff_bytes = 128 * 1024  # 128KB (used only to decide whether a file is text-like)

        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as http:
            async def _download_to_tempfile(
                url: str, *, suffix: str, max_bytes: int
            ) -> tuple[str, str | None, int]:
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix or ".bin")
                tmp_path = tmp.name
                size = 0
                content_type: str | None = None
                try:
                    async with http.stream("GET", url) as resp:
                        resp.raise_for_status()
                        content_type = resp.headers.get("content-type")
                        declared = resp.headers.get("content-length")
                        try:
                            declared_size = int(declared) if declared else None
                        except Exception:
                            declared_size = None
                        if declared_size is not None and declared_size > max_bytes:
                            raise ValueError(
                                f"File exceeds Gemini file-search per-document limit ({max_bytes} bytes): "
                                f"content-length={declared_size} url={url}"
                            )
                        async for chunk in resp.aiter_bytes():
                            if not chunk:
                                continue
                            size += len(chunk)
                            if size > max_bytes:
                                raise ValueError(
                                    f"File exceeds Gemini file-search per-document limit ({max_bytes} bytes): "
                                    f"downloaded={size} url={url}"
                                )
                            tmp.write(chunk)
                    tmp.close()
                    return tmp_path, content_type, size
                except Exception:
                    try:
                        tmp.close()
                    except Exception:
                        pass
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
                    raise

            def _file_looks_like_text(path: str) -> bool:
                try:
                    with open(path, "rb") as f:
                        head = f.read(sniff_bytes)
                except Exception:
                    return False
                return extract_text_fallback(head, max_chars=50_000) is not None

            for raw_url in urls:
                url = str(raw_url or "").strip()
                if not url:
                    continue

                if url in existing_source_urls:
                    new_docs.append({"source_url": url, "last_seen_turn": current_turn})
                    continue

                display_name = policy_display_name_from_url(url)
                # Initial guess from URL/filename; we refine after download with response headers.
                mime_guess = resolve_file_mime_type(file_name=display_name, source_url=url)
                decision = decide_auto_ingest(url=url, mime_type=mime_guess)
                custom_metadata = [
                    {"key": "source_url", "string_value": url},
                    {"key": "chat_id", "string_value": str(config.chat_id or "")},
                    {"key": "turn", "string_value": str(current_turn)},
                ]

                if decision.action == "skip_tabular":
                    new_docs.append(
                        build_manifest_entry(
                            url=url,
                            display_name=display_name,
                            status="skipped_tabular",
                            current_turn=current_turn,
                            metadata={"reason": decision.reason},
                            mime_type=mime_guess,
                        )
                    )
                    continue

                if decision.action == "skip_unsupported":
                    new_docs.append(
                        build_manifest_entry(
                            url=url,
                            display_name=display_name,
                            status="unsupported",
                            current_turn=current_turn,
                            metadata={"reason": decision.reason},
                            mime_type=mime_guess,
                        )
                    )
                    continue

                tmp_path: str | None = None
                try:
                    suffix = os.path.splitext(display_name)[1] if display_name else ""
                    tmp_path, content_type_header, file_size = await _download_to_tempfile(
                        url, suffix=suffix, max_bytes=max_upload_bytes
                    )

                    # Resolve mime_type to pass to File Search upload.
                    header_mt = normalize_mime_type(content_type_header)
                    resolved_mt = resolve_file_mime_type(
                        direct_candidates=[header_mt, decision.mime_type, mime_guess],
                        file_name=display_name,
                        source_url=url,
                    )
                    resolved_mt = normalize_mime_type(resolved_mt)

                    # Enforce File Search supported types. If we can't resolve a supported type, try a text fallback.
                    if not is_supported_file_search_mime_type(resolved_mt):
                        if _file_looks_like_text(tmp_path):
                            resolved_mt = "text/plain"
                        else:
                            new_docs.append(
                                build_manifest_entry(
                                    url=url,
                                    display_name=display_name,
                                    status="unsupported",
                                    current_turn=current_turn,
                                    metadata={
                                        "reason": decision.reason,
                                        "content_type": content_type_header,
                                        "file_size_bytes": file_size,
                                        "error": "unsupported-mime-type",
                                    },
                                    mime_type=resolved_mt or mime_guess,
                                )
                            )
                            continue

                    # Attach some extra metadata for debugging/audit.
                    custom_metadata = list(custom_metadata)
                    custom_metadata.append({"key": "mime_type", "string_value": str(resolved_mt or "")})
                    custom_metadata.append({"key": "file_size_bytes", "string_value": str(file_size)})
                    if content_type_header:
                        custom_metadata.append({"key": "http_content_type", "string_value": str(content_type_header)})

                    operation = await fs_client.upload_file_to_store(
                        file_path=tmp_path,
                        store_name=store_name,
                        display_name=display_name,
                        mime_type=resolved_mt,
                        custom_metadata=custom_metadata,
                    )
                    final_operation = await fs_client.wait_for_operation(operation)
                    payload = GeminiMessageNormalizer.normalize_response(final_operation)
                    response_payload = payload.get("response") if isinstance(payload, dict) else None
                    document_name = None
                    if isinstance(response_payload, dict):
                        document_name = response_payload.get("name")
                    if document_name is None and isinstance(payload, dict):
                        document_name = payload.get("name")

                    new_docs.append(
                        build_manifest_entry(
                            url=url,
                            display_name=display_name,
                            status="ingested",
                            current_turn=current_turn,
                            metadata={
                                "reason": decision.reason,
                                "content_type": content_type_header,
                                "file_size_bytes": file_size,
                            },
                            document_name=document_name,
                            mime_type=resolved_mt or mime_guess,
                        )
                    )
                except Exception as exc:
                    msg = str(exc)
                    logger.bind(ctx=ctx).exception("Gemini file-search store auto-index failed: {}", exc)
                    status = "failed"
                    if "per-document limit" in msg or "content-length" in msg and "limit" in msg:
                        status = "too_large"
                    new_docs.append(
                        build_manifest_entry(
                            url=url,
                            display_name=display_name,
                            status=status,
                            current_turn=current_turn,
                            metadata={"error": msg, "reason": decision.reason},
                            mime_type=decision.mime_type or mime_guess,
                        )
                    )
                finally:
                    if tmp_path:
                        try:
                            os.unlink(tmp_path)
                        except Exception:
                            pass

        if new_docs:
            config.file_search_store_manifest = self._merge_file_search_store_manifest(
                existing=existing_manifest,
                incoming=new_docs,
            )
        config.file_search_store_last_indexed_turn = current_turn


    def _apply_file_search_store_tool_state_update(
        self,
        *,
        config: GeminiChatConfig,
        tool_text: str,
        current_turn: int,
    ) -> None:
        apply_file_search_store_tool_state_update(
            config=config,
            tool_text=tool_text,
            current_turn=current_turn,
        )

    @staticmethod
    def _deliver_instructions(
        ctx: Context,
        config: GeminiChatConfig,
        instructions: list[RunInstruction],
        notify: Notify | None,
    ) -> None:
        for instruction in instructions:
            part = {"text": instruction.as_user_message()}
            last = config.messages[-1] if config.messages else None
            if isinstance(last, dict) and last.get("role") == "user" and isinstance(last.get("parts"), list):
                last["parts"].append(part)
            else:
                config.messages.append({"role": "user", "parts": [part]})
            logger.bind(ctx=ctx).info(f"Run instruction applied: {instruction.text}")
            notify(ctx, f"Instruction applied: {instruction.text}") if notify else None

    async def execute(
        self,
        *,
        ctx: Context,
        app_state: AppState,
        config: GeminiChatConfig,
        action_input: DataBlock,
        notify: Notify | None,
        output_text_to_file_char_limit: int | None,
    ) -> tuple[DataBlocks, GeminiChatConfig]:

        self.ensure_default_tools_registered()
        self._last_config = config
        self._ensure_action_run_scoped_chat_id(ctx=ctx, config=config)

        self.ensure_pinned_messages_initialized(config)
        self.rebuild_working_messages_from_history(config)
        config.messages = self._strip_file_data_parts_in_messages(config.messages or [])

        turn_start_index = len(config.messages or [])
        if app_state.cancellation is not None:
            app_state.cancellation.set_snapshot_meta(
                turn_start_index=turn_start_index,
                chat_turn=int(getattr(config, "chat_turn", 1) or 1),
            )
        current_turn = self._current_turn(config)
        config.current_turn_generated_files = []

        _tool_names, tool_defs = await self.resolve_tool_definitions(
            config=config,
            action_input=action_input,
        )
        generation_config = self.build_generation_config(config=config, tool_defs=tool_defs)

        user_message = await self.build_user_message(ctx=ctx, config=config, action_input=action_input)
        config.messages.append(user_message)

        loop = 0
        max_loops = 50
        turn_tool_results: list[dict[str, Any]] = []
        current_turn_generated_files: list[dict[str, Any]] = []
        final_response = None

        while loop < max_loops:
            app_state.raise_if_cancelled()
            snapshot_gemini(ctx, app_state, config, loop)
            self._deliver_instructions(ctx, config, await drain_instructions(app_state), notify)
            loop += 1
            before = GeminiMessageNormalizer.normalize_messages(config.messages)
            response = await self._create_or_stream_message(
                ctx=ctx,
                config=config,
                contents=before,
                generation_config=generation_config,
                notify=notify,
            )
            final_response = response
            TokenUsageService.record(
                ctx=ctx,
                surface=LlmSurface.gemini,
                app_state=app_state,
                usage=(
                    response.get("usage_metadata")
                    if isinstance(response, dict)
                    else getattr(response, "usage_metadata", None)
                ),
                model=str(config.model),
                loop=loop,
            )
            assistant_message = GeminiMessageNormalizer.first_candidate_content(response)
            if assistant_message is None:
                break
            config.messages.append(assistant_message)

            after = GeminiMessageNormalizer.normalize_messages(config.messages)
            GeminiThoughtSignatureHelper.validate_signature_count_preserved(
                before_messages=before,
                after_messages=after,
                model=config.model,
                provider=config.provider,
            )

            function_calls = GeminiMessageNormalizer.extract_function_calls(
                assistant_message.get("parts") or []
            )
            if not function_calls:
                break

            response_parts, persisted_results, generated_files = await self.run_tool_calls(
                ctx=ctx,
                app_state=app_state,
                function_calls=function_calls,
                notify=notify,
                output_text_to_file_char_limit=output_text_to_file_char_limit,
                step_index=loop,
            )
            turn_tool_results.extend(persisted_results)
            current_turn_generated_files.extend(generated_files)
            config.messages.append({"role": "user", "parts": response_parts})

        text = ""
        if final_response is not None:
            candidate_content = GeminiMessageNormalizer.first_candidate_content(final_response)
            if candidate_content is not None:
                text = GeminiMessageNormalizer.extract_text_from_parts(candidate_content.get("parts") or [])

        current_turn_generated_files = self._merge_generated_files_history([], current_turn_generated_files)
        config.current_turn_generated_files = current_turn_generated_files
        config.generated_files = self._merge_generated_files_history(
            config.generated_files,
            current_turn_generated_files,
        )
        generated_urls = self._dedupe_preserve_order([
            item.get("url") for item in current_turn_generated_files if isinstance(item, dict) and item.get("url")
        ])
        tool_urls = self._dedupe_preserve_order(
            [url for item in turn_tool_results for url in list(item.get("urls") or [])]
        )
        config.current_turn_urls = self._dedupe_preserve_order(list(config.current_turn_urls or []) + tool_urls + generated_urls)
        config.all_turn_urls = self._dedupe_preserve_order(list(config.all_turn_urls or []) + tool_urls + generated_urls)


        usage = self.extract_usage_metadata(final_response, turn=current_turn) if final_response is not None else {}
        self.append_current_turn_to_history(
            config=config,
            turn=current_turn,
            start_index=turn_start_index,
            tool_results=turn_tool_results,
            usage=usage,
        )
        self.minimize_persisted_messages(config)

        await self._maybe_index_turn_history_to_vector_store(
            ctx=ctx,
            config=config,
            notify=notify,
            current_turn=current_turn,
        )

        config.tool_results = turn_tool_results
        config.tool_results_history = [
            *GeminiMessageNormalizer.to_plain(config.tool_results_history or []),
            *turn_tool_results,
        ][-2000:]
        if usage:
            config.usage_metadata_history = [
                *GeminiMessageNormalizer.to_plain(config.usage_metadata_history or []),
                usage,
            ][-200:]
        config.chat_turn = current_turn + 1

        data_blocks = DataBlocks(blocks=[])
        if text or generated_urls:
            data_blocks.blocks.append(DataBlock(text=text, urls=generated_urls))
        return data_blocks, config
