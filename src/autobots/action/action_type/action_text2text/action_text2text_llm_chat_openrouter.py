import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Type
from urllib.parse import unquote, urlparse

from fastapi import HTTPException
from loguru import logger
from openai.types.chat import ChatCompletionToolParam

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import (
    ActionABC,
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
    ActionInputType,
    ActionOutputType,
)
from src.autobots.action.action_type.action_text2text.action_convert_file_to_pdf_or_txt import (
    ActionText2TextConvertToPDForText,
    ConvertConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.openrouter.chat_model import OpenRouterChatReq
from src.autobots.conn.openrouter.model_capabilities import get_model_capabilities
from src.autobots.conn.openrouter.pipeline.turn_pipeline import OpenRouterTurnPipeline, TurnContext
from src.autobots.conn.openrouter.utils import (
    dedupe_urls,
    is_tool_present,
    strip_reasoning_fields,
    to_plain,
)
from src.autobots.conn.openrouter.vector_store.manifest_manager import VectorStoreManifestManager
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.files.file_downloader import FileDownloader
from src.autobots.llm.tools.common_default_tools import OPENROUTER_DEFAULT_TOOLS
from src.autobots.llm.tools.openai_vector_store_tool import (
    OPENAI_VECTOR_STORE_AUTO_INDEX_DENY_EXTENSIONS,
    OPENAI_VECTOR_STORE_SUPPORTED_EXTENSIONS,
    OpenAIVectorStoreFileInput,
    OpenAIVectorStoreToolConfig,
    OpenAIVectorStoreToolOperation,
    openai_vector_store_tool,
)
from src.autobots.llm.tools.tool_factory_openrouter import ToolFactoryOpenRouter
from src.autobots.llm.tools.tools_map import TOOLS_MAP
from src.autobots.mcp.mcp_client import MCPToolBinding
from src.autobots.mcp.mcp_doc_model import MCP_START_CONSTANT, MCPDoc
from src.autobots.mcp.user_mcp import UserMCP
from src.autobots.server_sent_events.server_sent_events import Notify


class OpenRouterChatConfig(OpenRouterChatReq):
    tool_output_text_to_file_char_limit: int | None = 50000
    tool_input_text_to_file_char_limit: int | None = 20000

    supports_image: bool = True

    # Default tools (config-driven).
    tools: List[ChatCompletionToolParam | str | Dict[str, Any]] | None = list(OPENROUTER_DEFAULT_TOOLS)


class ActionText2TextLlmChatOpenRouter(
    ActionABC[
        OpenRouterChatConfig,
        OpenRouterChatConfig,
        OpenRouterChatConfig,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.text2text_llm_chat_openrouter

    @staticmethod
    def get_description() -> str:
        return (
            "Runs an OpenRouter (OpenAI-compatible) chat completion with server-side tool calling. "
            "This action is config-driven and keeps bounded visible context in messages while persisting "
            "full turn history and tool results out-of-band.\n\n"
            "Default tools include openai_vector_store_tool, daytona_sandbox, and openrouter_inspect."
        )

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return OpenRouterChatConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return OpenRouterChatConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return OpenRouterChatConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    def _strip_reasoning_fields_from_persisted_state(self) -> None:
        if not bool(getattr(self.action_config, "reasoning_exclude", True)):
            return

        # Clean the working messages list
        for msg in getattr(self.action_config, "messages", None) or []:
            strip_reasoning_fields(msg)

        # Clean pinned messages
        for msg in getattr(self.action_config, "pinned_messages", None) or []:
            strip_reasoning_fields(msg)

        # Clean turn history messages
        turn_history = getattr(self.action_config, "turn_history", None) or []
        for turn in turn_history:
            if hasattr(turn, "messages"):
                for msg in getattr(turn, "messages", None) or []:
                    strip_reasoning_fields(msg)
            elif isinstance(turn, dict):
                for msg in (turn.get("messages") or []):
                    strip_reasoning_fields(msg)

    @staticmethod
    def _remove_tool_by_name(tool_defs: list[Any] | None, name: str) -> list[Any]:
        """Return tool_defs with any tool whose name matches `name` removed.

        Handles both bare `{"name": ...}` and OpenAI-style
        `{"type": "function", "function": {"name": ...}}` shapes.
        """
        result: list[Any] = []
        for tool in tool_defs or []:
            if isinstance(tool, dict):
                if tool.get("name") == name:
                    continue
                fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
                if fn.get("name") == name:
                    continue
            result.append(tool)
        return result

    # OpenAI Vector Store orchestration
    @staticmethod
    def _display_name_from_url(url: str) -> str:
        parsed = urlparse(str(url or ""))
        return unquote(Path(parsed.path or "").name) or str(url or "unknown_file")

    @staticmethod
    def _extract_extension_from_url(url: str) -> str:
        parsed = urlparse(str(url or ""))
        return Path(unquote(parsed.path or "")).suffix.lower()

    def _extract_json_payload_from_datablocks(self, data_blocks: DataBlocks | Exception) -> dict[str, Any]:
        if isinstance(data_blocks, Exception):
            raise data_blocks
        if not data_blocks.blocks or not data_blocks.blocks[0].text:
            raise HTTPException(status_code=500, detail="Expected JSON payload in DataBlocks")
        return json.loads(data_blocks.blocks[0].text)

    async def _compute_source_url_sha256(
        self,
        ctx: Context,
        source_url: str,
    ) -> str | None:
        downloaded_files = await FileDownloader._download_urls_to_tmpfiles(
            ctx=ctx,
            user=self.user,
            db=self.db,
            urls=[source_url],
        )

        local_path = downloaded_files.get(source_url)
        if not local_path:
            return None

        try:
            hasher = hashlib.sha256()
            with open(local_path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    hasher.update(chunk)
            return hasher.hexdigest()
        finally:
            if local_path and os.path.exists(local_path):
                os.remove(local_path)

    async def _convert_url_to_vector_store_ready_url(
        self,
        ctx: Context,
        app_state: AppState,
        url: str,
    ) -> str | None:
        action_input = DataBlock(urls=[url])
        action = ActionText2TextConvertToPDForText(
            action_config=ConvertConfig(url=""),
            user=self.user,
            db=self.db,
        )
        result = await action.run_action(ctx=ctx, app_state=app_state, action_input=action_input)
        if isinstance(result, Exception):
            raise result

        if not result.blocks or not result.blocks[0].urls:
            return None

        return result.blocks[0].urls[0]

    async def _prepare_vector_store_url(
        self,
        ctx: Context,
        app_state: AppState,
        source_url: str,
    ) -> tuple[str | None, str, str, str | None]:
        ext = self._extract_extension_from_url(source_url).lower()

        if ext in OPENAI_VECTOR_STORE_AUTO_INDEX_DENY_EXTENSIONS:
            return (
                None,
                "tabular_container_only",
                "unsupported",
                "Tabular files are not auto-indexed into OpenAI vector store",
            )

        if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            return None, "image_only", "unsupported", "Images are not indexed into vector store in this flow"

        if ext in OPENAI_VECTOR_STORE_SUPPORTED_EXTENSIONS:
            return source_url, "native_vector_store", "prepared", None

        try:
            converted_url = await self._convert_url_to_vector_store_ready_url(ctx=ctx, app_state=app_state, url=source_url)
        except Exception as exc:
            return None, "container_only", "failed", str(exc)

        converted_ext = self._extract_extension_from_url(converted_url).lower() if converted_url else ""
        if converted_url and converted_ext in OPENAI_VECTOR_STORE_SUPPORTED_EXTENSIONS:
            return converted_url, "converted_txt_vector_store", "prepared", None

        return None, "container_only", "unsupported", "Could not prepare a vector-store-compatible representation"

    async def _ensure_vector_store_exists(self, ctx: Context, app_state: AppState, notify: Notify | None = None) -> None:
        if not getattr(self.action_config, "vector_store_enabled", True):
            return

        if getattr(self.action_config, "vector_store_id", None):
            return

        if not is_tool_present(getattr(self.action_config, "tools", None), "openai_vector_store_tool"):
            return

        name = getattr(self.action_config, "vector_store_name", None) or f"openrouter-chat-{gen_uuid()}"
        # TODO: AUT-1459 : replace openai_vector_store_tool with a system-creds-friendly
        # file search (Chroma/Weaviate + OpenRouter embeddings); it needs per-user OpenAI
        # creds that OpenRouter product-subscriber (system-creds) runs lack.
        result = await openai_vector_store_tool(
            config=OpenAIVectorStoreToolConfig(
                operation=OpenAIVectorStoreToolOperation.CREATE_VECTOR_STORE,
                name=name,
            ),
            ctx=ctx,
            app_state=app_state,
            user=self.user,
            db=self.db,
            notify=notify,
        )
        payload = self._extract_json_payload_from_datablocks(result)
        vector_store = payload.get("vector_store") if isinstance(payload.get("vector_store"), dict) else payload
        vector_store_id = str(vector_store.get("id") or "").strip()
        if not vector_store_id:
            raise HTTPException(status_code=500, detail="Failed to create vector store")

        self.action_config.vector_store_id = vector_store_id
        self.action_config.vector_store_name = vector_store.get("name") or name

    async def _prepare_vector_store_state(self, ctx: Context, app_state: AppState, action_input: DataBlock) -> None:
        if not getattr(self.action_config, "vector_store_enabled", True):
            return

        if not is_tool_present(getattr(self.action_config, "tools", None), "openai_vector_store_tool"):
            return

        # Ensure URLs extracted from text are reflected in action_input.urls (if enabled upstream).
        action_input.get_llm_prompt(ctx=ctx)

        turn = int(getattr(self.action_config, "chat_turn", 1) or 1)
        current_turn_urls = dedupe_urls(list(action_input.urls or []))

        manifest = to_plain(getattr(self.action_config, "vector_store_manifest", None) or [])
        current_turn_vector_store_urls: list[str] = []

        for source_url in current_turn_urls:
            existing = VectorStoreManifestManager.find_entry(manifest, source_url=source_url)

            current_source_sha256 = await self._compute_source_url_sha256(ctx=ctx, source_url=source_url)
            existing_source_sha256 = str(existing.get("source_content_sha256") or "").strip() if existing else ""

            content_changed = bool(existing and current_source_sha256 and existing_source_sha256 != current_source_sha256)

            prepared_url = existing.get("prepared_url") if existing and not content_changed else None
            representation = existing.get("representation") if existing else None
            status = existing.get("status") if existing else None
            last_error = existing.get("last_error") if existing else None

            if not prepared_url and status not in {"unsupported", "failed"}:
                prepared_url, representation, prepared_status, last_error = await self._prepare_vector_store_url(
                    ctx=ctx,
                    app_state=app_state,
                    source_url=source_url,
                )
                status = "stale" if content_changed else prepared_status

            if prepared_url:
                current_turn_vector_store_urls.append(prepared_url)

            manifest = VectorStoreManifestManager.upsert_entry(
                manifest,
                source_url=source_url,
                prepared_url=prepared_url,
                source="user_input",
                representation=representation or "native_vector_store",
                status=status or "discovered",
                turn=turn,
                display_name=self._display_name_from_url(source_url),
                last_error=last_error,
                source_content_sha256=current_source_sha256,
            )

        self.action_config.current_turn_vector_store_urls = dedupe_urls(
            current_turn_vector_store_urls
        )
        self.action_config.vector_store_manifest = manifest

    def _apply_vector_store_add_payload_to_manifest(
        self,
        *,
        payload: dict[str, Any],
        prepared_to_source_url: dict[str, str],
        turn: int,
    ) -> None:
        manifest = to_plain(getattr(self.action_config, "vector_store_manifest", None) or [])

        successful_openai_map = {
            str(x.get("source") or "").strip(): str(
                x.get("openai_file_id") or x.get("file_id") or ""
            ).strip()
            for x in payload.get("successful_files", [])
            if isinstance(x, dict)
            and str(x.get("source") or "").strip()
            and str(x.get("openai_file_id") or x.get("file_id") or "").strip()
        }
        reused_openai_map = {
            str(x.get("source") or "").strip(): str(
                x.get("openai_file_id") or x.get("file_id") or ""
            ).strip()
            for x in payload.get("reused_files", [])
            if isinstance(x, dict)
            and str(x.get("source") or "").strip()
            and str(x.get("openai_file_id") or x.get("file_id") or "").strip()
        }
        failed_map = {
            str(x.get("source") or "").strip(): x.get("reason")
            for x in payload.get("failed_files", [])
            if isinstance(x, dict) and str(x.get("source") or "").strip()
        }

        indexed_entries = payload.get("indexed_files", []) or []
        indexed_by_prepared_url = {
            str(x.get("prepared_url") or "").strip(): x
            for x in indexed_entries
            if isinstance(x, dict) and str(x.get("prepared_url") or "").strip()
        }

        batch = payload.get("batch") if isinstance(payload.get("batch"), dict) else {}
        batch_status = str(batch.get("status") or "").strip().lower()
        default_status = "indexed" if batch_status == "completed" else "pending"

        for prepared_url, source_url in prepared_to_source_url.items():
            indexed_entry = indexed_by_prepared_url.get(prepared_url, {})

            vector_store_file_id = str(indexed_entry.get("vector_store_file_id") or "").strip() or None

            openai_file_id = str(
                indexed_entry.get("openai_file_id")
                or indexed_entry.get("source_openai_file_id")
                or successful_openai_map.get(prepared_url)
                or reused_openai_map.get(prepared_url)
                or ""
            ).strip() or None

            error_reason = failed_map.get(prepared_url)

            raw_status = str(indexed_entry.get("status") or default_status).strip().lower()
            status = "failed" if error_reason else ("indexed" if raw_status in {"indexed", "completed"} else raw_status)

            attributes = indexed_entry.get("attributes") if isinstance(indexed_entry.get("attributes"), dict) else {}
            source_content_sha256 = str(attributes.get("source_content_sha256") or "").strip() or None

            manifest = VectorStoreManifestManager.upsert_entry(
                manifest,
                source_url=source_url,
                prepared_url=prepared_url,
                source="user_input",
                representation=(
                    "native_vector_store" if prepared_url == source_url else "converted_txt_vector_store"
                ),
                status=status,
                turn=turn,
                display_name=self._display_name_from_url(source_url),
                last_error=error_reason,
                source_content_sha256=source_content_sha256,
                vector_store_file_id=vector_store_file_id,
                openai_file_id=openai_file_id,
                last_indexed_turn=turn if status == "indexed" else None,
            )

        self.action_config.vector_store_manifest = manifest

    async def _auto_index_current_turn_urls_to_vector_store(
        self,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> None:
        if not getattr(self.action_config, "vector_store_enabled", True):
            return

        if not is_tool_present(getattr(self.action_config, "tools", None), "openai_vector_store_tool"):
            return

        vector_store_id = getattr(self.action_config, "vector_store_id", None)
        if not vector_store_id:
            return

        manifest = to_plain(getattr(self.action_config, "vector_store_manifest", None) or [])
        turn = int(getattr(self.action_config, "chat_turn", 1) or 1)

        files_to_add: list[OpenAIVectorStoreFileInput] = []
        prepared_to_source_url: dict[str, str] = {}
        delete_file_ids: list[str] = []

        for source_url in list(getattr(self.action_config, "current_turn_urls", None) or []):
            entry = VectorStoreManifestManager.find_entry(manifest, source_url=source_url)
            if not entry:
                continue

            prepared_url = entry.get("prepared_url")
            status = str(entry.get("status") or "")
            source_content_sha256 = entry.get("source_content_sha256")
            stale_vector_store_file_id = str(entry.get("vector_store_file_id") or "").strip()

            if not prepared_url:
                continue

            if status in {"indexed", "pending"}:
                continue

            if status == "stale" and stale_vector_store_file_id:
                delete_file_ids.append(stale_vector_store_file_id)

            prepared_to_source_url[prepared_url] = source_url
            files_to_add.append(
                OpenAIVectorStoreFileInput(
                    url=prepared_url,
                    attributes={
                        "source_url": source_url,
                        "prepared_url": prepared_url,
                        "turn": turn,
                        "source_content_sha256": source_content_sha256 or "",
                    },
                )
            )

        if not files_to_add:
            return

        delete_file_ids = dedupe_urls(delete_file_ids)
        operation = (
            OpenAIVectorStoreToolOperation.REINDEX_FILES
            if delete_file_ids
            else OpenAIVectorStoreToolOperation.ADD_FILES
        )

        # TODO: AUT-1459 : replace openai_vector_store_tool with a system-creds-friendly
        # file search (Chroma/Weaviate + OpenRouter embeddings); it needs per-user OpenAI
        # creds that OpenRouter product-subscriber (system-creds) runs lack.
        result = await openai_vector_store_tool(
            config=OpenAIVectorStoreToolConfig(
                operation=operation,
                vector_store_id=vector_store_id,
                files=files_to_add,
                delete_file_ids=delete_file_ids,
                delete_underlying_openai_files=False,
                wait_for_completion=True,
            ),
            ctx=ctx,
            app_state=app_state,
            user=self.user,
            db=self.db,
            notify=notify,
        )
        payload = self._extract_json_payload_from_datablocks(result)

        add_payload = payload
        if operation == OpenAIVectorStoreToolOperation.REINDEX_FILES:
            add_payload = payload.get("add_result") or {}

        if add_payload:
            self._apply_vector_store_add_payload_to_manifest(
                payload=add_payload,
                prepared_to_source_url=prepared_to_source_url,
                turn=turn,
            )

    async def _validate_and_adjust_model_capabilities(self, ctx: Context, notify: Notify | None = None) -> None:
        model_id = getattr(self.action_config, "model", None)
        if not model_id:
            logger.warning("No model specified in action config, skipping capability validation")
            return

        model_info = await get_model_capabilities(model_id)
        if model_info is None:
            logger.bind(ctx=ctx, model=model_id).warning(
                f"Model '{model_id}' is not in the OpenRouter catalogue; "
                "leaving the config untouched and letting OpenRouter validate it"
            )
            return

        capabilities = model_info.capabilities
        
        original_supports_image = getattr(self.action_config, "supports_image", True)
        original_tools_count = len(getattr(self.action_config, "tools", None) or [])
        original_tool_choice = getattr(self.action_config, "tool_choice", None)
        
        adjustments_made = []
        
        if not capabilities.vision and original_supports_image:
            self.action_config.supports_image = False
            adjustments_made.append("supports_image: True -> False (model doesn't support vision)")
            logger.bind(
                ctx=ctx,
                model=model_id,
                adjustment="supports_image",
                original=original_supports_image,
                adjusted=False,
            ).info("Adjusted supports_image to False: model doesn't support vision")
            
            if notify:
                notify(ctx, f"Note: Model '{model_id}' doesn't support vision. Image processing disabled.")
        
        # Adjust tool calling support
        if not capabilities.tool_calling and original_tools_count > 0:
            self.action_config.tools = []
            self.action_config.tool_choice = "none"
            adjustments_made.append(f"tools: {original_tools_count} tools -> [] (model doesn't support tool calling)")
            adjustments_made.append(f"tool_choice: {original_tool_choice} -> 'none'")
            
            logger.bind(
                ctx=ctx,
                model=model_id,
                adjustment="tools",
                original_tools_count=original_tools_count,
                adjusted_tools_count=0,
                original_tool_choice=original_tool_choice,
                adjusted_tool_choice="none",
            ).info("Adjusted tools to [] and tool_choice to 'none': model doesn't support tool calling")
            
            if notify:
                notify(ctx, f"Note: Model '{model_id}' doesn't support tool calling. Tools disabled.")
        
        if adjustments_made:
            logger.bind(
                ctx=ctx,
                model=model_id,
                model_name=model_info.name,
                adjustments=adjustments_made,
            ).info(f"Model capability adjustments applied for {model_id}")

    def _current_turn(self) -> int:
        return int(getattr(self.action_config, "chat_turn", 1) or 1)

    def _visible_turns(self) -> int:
        raw = getattr(self.action_config, "visible_turns", 20)
        try:
            value = int(raw)
        except Exception:
            value = 20
        return max(1, min(200, value))

    def _ensure_pinned_messages_initialized(self) -> None:
        if self._current_turn() != 1:
            return
        pinned = getattr(self.action_config, "pinned_messages", None) or []
        if pinned:
            return
        self.action_config.pinned_messages = to_plain(getattr(self.action_config, "messages", None) or [])

    def _include_previous_tool_messages(self) -> bool:
        return bool(getattr(self.action_config, "include_previous_tool_messages", False))

    def _previous_tool_messages_turns(self) -> int:
        value = getattr(self.action_config, "previous_tool_messages_turns", None)
        if value is None:
            return 5
        visible = self._visible_turns()
        return min(int(value), visible)

    def _filter_history_messages_for_visible_window(self, msgs: list[Any]) -> list[Any]:
        filtered: list[Any] = []
        for raw in msgs or []:
            if not isinstance(raw, dict):
                filtered.append(raw)
                continue
            role = raw.get("role")
            if role in {"tool", "function"}:
                continue
            if role == "assistant" and (raw.get("tool_calls") or raw.get("function_call")):
                continue
            filtered.append(raw)
        return filtered

    def _rebuild_working_messages_from_history(self) -> None:
        pinned = to_plain(getattr(self.action_config, "pinned_messages", None) or [])
        pinned = self._filter_history_messages_for_visible_window(list(pinned or []))
        history = to_plain(getattr(self.action_config, "turn_history", None) or [])
        visible = self._visible_turns()

        window = history[-visible:] if history else []
        include_tool_msgs = self._include_previous_tool_messages()
        tool_turns = self._previous_tool_messages_turns() if include_tool_msgs else 0
        
        flattened: list[Any] = []
        for i, raw_turn in enumerate(window):
            if not isinstance(raw_turn, dict):
                continue
            msgs = raw_turn.get("messages") or []
            if isinstance(msgs, list):
                if include_tool_msgs and tool_turns > 0 and i >= len(window) - tool_turns:
                    flattened.extend(msgs)
                else:
                    flattened.extend(self._filter_history_messages_for_visible_window(msgs))

        self.action_config.messages = pinned + flattened

    def _append_current_turn_to_history(self, *, turn: int, start_index: int) -> bool:
        if start_index < 0:
            return False
        all_msgs = list(getattr(self.action_config, "messages", None) or [])
        delta = to_plain(all_msgs[start_index:])
        if not delta:
            return False

        history = to_plain(getattr(self.action_config, "turn_history", None) or [])
        history.append({"turn": int(turn), "messages": delta})
        self.action_config.turn_history = history
        return True

    def _merge_tool_results_history(
        self,
        previous_items: list[Any] | None,
        current_items: list[Any] | None,
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()

        for raw in (previous_items or []) + (current_items or []):
            item = to_plain(raw)
            if not isinstance(item, dict):
                continue
            tool_call_id = str(item.get("tool_call_id") or "")
            if not tool_call_id or tool_call_id in seen:
                continue
            seen.add(tool_call_id)
            merged.append(item)

        return merged[-2000:]

    @staticmethod
    def _tool_schema_profile_for_model(model_id: str | None) -> str:
        mid = str(model_id or "").strip().lower()
        if mid.startswith("xai-") or mid.startswith("x-ai/") or mid.startswith("xai/"):
            return "no_refs"
        return "default"

    @staticmethod
    async def _replace_tools_with_defs(
        *,
        ctx: Context,
        tools: list[Any] | None,
        model_id: str | None = None,
    ) -> list[dict[str, Any]] | None:
        if not tools:
            return None

        tool_names: list[str] = []
        tool_defs: list[dict[str, Any]] = []

        for tool in tools:
            if isinstance(tool, str):
                if tool.startswith(MCP_START_CONSTANT):
                    # MCP reference: kept verbatim so it stays in the persisted config.
                    # Its tools are resolved per run by _resolve_mcp_tool_defs, since the
                    # remote server's tool list can change between turns.
                    tool_defs.append(tool)
                elif tool in TOOLS_MAP:
                    tool_names.append(tool)
                else:
                    # If a tool name isn't registered, keep it as-is (defensive).
                    tool_defs.append({"type": "function", "function": {"name": tool}})
            elif isinstance(tool, dict):
                tool_defs.append(dict(tool))
            elif hasattr(tool, "model_dump"):
                tool_defs.append(tool.model_dump(exclude_none=True))

        if tool_names:
            schema_profile = ActionText2TextLlmChatOpenRouter._tool_schema_profile_for_model(model_id)
            resolved = await ToolFactoryOpenRouter.get_chat_completion_tool_param(
                ctx=ctx,
                tool_names=tool_names,
                schema_profile=schema_profile,
            )
            if resolved:
                for t in resolved:
                    if hasattr(t, "model_dump"):
                        tool_defs.append(t.model_dump(exclude_none=True))
                    else:
                        tool_defs.append(dict(t))

        return tool_defs if tool_defs else None

    async def _resolve_mcp_tool_defs(
        self,
        *,
        ctx: Context,
        tool_defs: list[Any] | None,
    ) -> tuple[list[Any], dict[str, MCPToolBinding]]:
        """Expand `mcp_<server>` references into concrete tool definitions.

        OpenRouter has no server-side MCP support, so each referenced server is queried
        for its tool list and those tools are advertised as ordinary functions. The
        returned list replaces the references and is used for this run's payload only;
        the references themselves stay in the persisted config so a later turn re-resolves
        against the server's current tools.

        A server that cannot be reached is dropped from this run rather than failing it.
        """
        resolved: list[Any] = []
        server_names: list[str] = []

        for tool in tool_defs or []:
            if isinstance(tool, str) and tool.startswith(MCP_START_CONSTANT):
                server_name = tool[len(MCP_START_CONSTANT):].strip()
                if server_name and server_name not in server_names:
                    server_names.append(server_name)
            else:
                resolved.append(tool)

        if not server_names:
            return resolved, {}

        user = getattr(self, "user", None)
        db = getattr(self, "db", None)
        if user is None or db is None:
            logger.bind(ctx=ctx).warning(
                "No user/db available to resolve MCP servers; skipping MCP for this run"
            )
            return resolved, {}

        user_mcp = UserMCP(user=user, db=db)
        mcp_docs: list[MCPDoc] = []
        for server_name in server_names:
            try:
                mcp_doc = await user_mcp.get_mcp_by_server_name(
                    ctx=ctx, mcp_server_name=server_name, decrypt=True
                )
            except Exception as e:
                logger.bind(ctx=ctx, mcp_server_name=server_name).warning(
                    f"Could not load MCP server {server_name}; skipping for this run: {str(e)}"
                )
                continue
            if mcp_doc:
                mcp_docs.append(mcp_doc)
            else:
                logger.bind(ctx=ctx, mcp_server_name=server_name).warning(
                    "Configured MCP server not found; skipping for this run"
                )

        if not mcp_docs:
            return resolved, {}

        tool_params, bindings = await ToolFactoryOpenRouter.get_mcp_chat_completion_tool_param(
            ctx=ctx, mcp_docs=mcp_docs
        )

        existing_names = {
            name
            for tool in resolved
            if isinstance(tool, dict)
            and (name := (tool.get("function") or {}).get("name")) is not None
        }
        for tool_param in tool_params:
            if tool_param["function"]["name"] in existing_names:
                continue
            resolved.append(dict(tool_param))

        return resolved, bindings

    def _persist_mcp_references(self, bindings: dict[str, MCPToolBinding] | None) -> None:
        """Record `mcp_<server>` references for the MCP servers used in this run.

        An MCP enabled mid-run via `enable_mcp` is otherwise only present on the
        transient request, so it would be lost on the next turn. Persisting the
        reference (never the resolved tool definitions) keeps it available while still
        re-resolving each turn.

        Servers already referenced in the config are left untouched, so this is safe to
        call on every run.
        """
        if not bindings:
            return

        tools: list[Any] = list(self.action_config.tools or [])
        existing_refs = {t for t in tools if isinstance(t, str)}

        added: list[str] = []
        for binding in bindings.values():
            ref = f"{MCP_START_CONSTANT}{binding.server_name}"
            if ref in existing_refs:
                continue
            existing_refs.add(ref)
            tools.append(ref)
            added.append(ref)

        if added:
            self.action_config.tools = tools

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        try:
            logger.bind(
                ctx=ctx,
                action_input_text_len=len(action_input.text or ""),
                action_input_urls_count=len(action_input.urls or []),
                action_input_tools_count=len(action_input.tools or []),
            ).info("Starting OpenRouter run_action")

            turn = TurnContext(
                action=self,
                ctx=ctx,
                app_state=app_state,
                action_input=action_input,
                notify=notify,
            )
            return await OpenRouterTurnPipeline().run(turn)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in OpenRouter run_action: {str(e)}")
            return e

    async def run_tool(
        self,
        action_config: OpenRouterChatConfig,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        raise NotImplementedError
