import hashlib
import inspect
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Type
from urllib.parse import unquote, urlparse

from anthropic.types import TextBlock
from anthropic.types.beta import (
    BetaMemoryTool20250818Param,
    BetaTextBlock,
)
from fastapi import HTTPException
from loguru import logger
from src.autobots.action.action.action_doc_model import ActionResult
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
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.claude.chat_model import ChatReqClaude, ClaudeMessageParam
from src.autobots.conn.claude.claude_client import Claude
from src.autobots.conn.claude.model_registry import ClaudeModelRegistry, ClaudeProvider
from src.autobots.conn.openai.openai_files.openai_files import OpenaiFiles
from src.autobots.conn.openai.openai_files.openai_files_model import FileCreate
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.files.file_downloader import FileDownloader
from src.autobots.llm.tools.common_default_tools import CLAUDE_DEFAULT_TOOLS
from src.autobots.llm.tools.openai_vector_store_tool import (
    OPENAI_VECTOR_STORE_AUTO_INDEX_DENY_EXTENSIONS,
    OPENAI_VECTOR_STORE_SUPPORTED_EXTENSIONS,
    OpenAIVectorStoreFileInput,
    OpenAIVectorStoreToolConfig,
    OpenAIVectorStoreToolOperation,
    openai_vector_store_tool,
)
from src.autobots.llm.tools.tool_factory_claude import ToolFactoryClaude
from src.autobots.llm.tools.tools_map import TOOLS_MAP
from src.autobots.mcp.mcp_doc_model import MCP_START_CONSTANT
from src.autobots.mcp.user_mcp import UserMCP
from src.autobots.server_sent_events.server_sent_events import Notify


class ClaudeChatConfig(ChatReqClaude):
    tool_output_text_to_file_char_limit: int | None = 50000


class ActionText2TextLlmChatclaude(
    ActionABC[ClaudeChatConfig, ClaudeChatConfig, ClaudeChatConfig, DataBlock, DataBlocks]
):
    type = ActionType.text2text_llm_chat_claude

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

    def _is_openai_vector_store_tool_requested(self, action_input: DataBlock | None = None) -> bool:
        if not getattr(self.action_config, "vector_store_enabled", True):
            return False
        tools: list[Any] = []
        tools.extend(self.action_config.tools or [])
        if action_input is not None:
            tools.extend(action_input.tools or [])
        for tool in tools:
            if isinstance(tool, str) and tool == "openai_vector_store_tool":
                return True
            if isinstance(tool, dict) and tool.get("name") == "openai_vector_store_tool":
                return True
        return False

    def _is_openai_vector_store_tool_present_in_current_tools(self) -> bool:
        for tool in self.action_config.tools or []:
            if isinstance(tool, str) and tool == "openai_vector_store_tool":
                return True
            if isinstance(tool, dict) and tool.get("name") == "openai_vector_store_tool":
                return True
        return False
    
    def _is_claude_container_file_attach_present_in_current_tools(self) -> bool:
        for tool in self.action_config.tools or []:
            if isinstance(tool, str) and tool == "claude_container_file_attach":
                return True
            if isinstance(tool, dict) and tool.get("name") == "claude_container_file_attach":
                return True
        return False

    def _append_beta(self, beta_name: str) -> None:
        self.action_config.betas = list(dict.fromkeys((self.action_config.betas or []) + [beta_name]))

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
        """Capture the turn-1 prompt prefix (system-like messages) exactly once.

        This intentionally happens *before* we append the turn-1 user prompt.
        """
        if self._current_turn() != 1:
            return

        pinned = getattr(self.action_config, "pinned_messages", None) or []
        if pinned:
            return

        self.action_config.pinned_messages = self._to_plain(
            getattr(self.action_config, "messages", None) or []
        )

    def _rebuild_working_messages_from_history(self) -> None:
        pinned = self._to_plain(getattr(self.action_config, "pinned_messages", None) or [])
        history = self._to_plain(getattr(self.action_config, "turn_history", None) or [])
        visible = self._visible_turns()

        window = history[-visible:] if history else []
        flattened: list[Any] = []
        for raw_turn in window:
            if not isinstance(raw_turn, dict):
                continue
            msgs = raw_turn.get("messages") or []
            if isinstance(msgs, list):
                flattened.extend(msgs)

        self.action_config.messages = pinned + flattened

    def _content_has_non_empty_user_payload(self, content: Any) -> bool:
        if content is None:
            return False

        if isinstance(content, str):
            return bool(content.strip())

        if isinstance(content, list):
            if not content:
                return False
            for raw_block in content:
                block = self._to_plain(raw_block)
                # Non-dict blocks are treated as non-empty (defensive; should be rare).
                if not isinstance(block, dict):
                    return True
                block_type = str(block.get("type") or "")
                if block_type == "text":
                    if str(block.get("text") or "").strip():
                        return True
                    continue
                # Any non-text block (tool_result, image, document, etc.) counts as content.
                return True
            return False

        # Unknown content types: treat as non-empty to avoid accidental drops.
        return True

    def _prune_empty_user_messages(self) -> None:
        cleaned: list[Any] = []
        for raw in (getattr(self.action_config, "messages", None) or []):
            msg = self._to_plain(raw)
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "user" and not self._content_has_non_empty_user_payload(msg.get("content")):
                continue
            cleaned.append(msg)
        self.action_config.messages = cleaned


    def _append_current_turn_to_history(self, *, turn: int, start_index: int) -> bool:
        if start_index < 0:
            return False

        all_msgs = list(getattr(self.action_config, "messages", None) or [])
        delta = self._to_plain(all_msgs[start_index:])
        if not delta:
            return False

        history = self._to_plain(getattr(self.action_config, "turn_history", None) or [])
        history.append({"turn": int(turn), "messages": delta})
        self.action_config.turn_history = history
        return True

    def _thinking_type(self) -> str | None:
        thinking = getattr(self.action_config, "thinking", None)
        if isinstance(thinking, dict):
            return str(thinking.get("type"))
        return None

    def _delta_has_role(self, *, start_index: int, role: str) -> bool:
        if start_index < 0:
            return False

        all_msgs = list(getattr(self.action_config, "messages", None) or [])
        for raw in all_msgs[start_index:]:
            msg = self._to_plain(raw)
            if isinstance(msg, dict) and msg.get("role") == role:
                return True
        return False

    def _append_synthetic_assistant_failure_message(self, *, kind: str) -> None:
        # Keep this short + non-leaky; do not include stack traces or provider details.
        text = (
            "(System note) The previous turn failed due to a runtime error. "
            "Please retry only if user asked to retry."
        )

        self.action_config.messages.append(
            ClaudeMessageParam(
                role="assistant",
                content=[{"type": "text", "text": text}],
            )
        )

    def _tools_present_for_request(self) -> bool:
        return bool(self.action_config.tools)

    def _minimize_persisted_messages(self) -> None:
        """Avoid duplicating recent turns in persisted config.

        `turn_history` + `pinned_messages` are the canonical store for verbatim history.
        `messages` is treated as a working-set window that is rebuilt at runtime each turn.
        """
        self.action_config.messages = []

    def _supports_dynamic_web_filtering(self) -> bool:
        provider = self._current_provider()
        model = self._current_model()

        if provider != ClaudeProvider.ANTHROPIC:
            return False

        return model.startswith("claude-opus-4-6") or model.startswith("claude-sonnet-4-6")

    def _should_force_direct_latest_web_tools(self) -> bool:
        return not self._supports_dynamic_web_filtering()
    
    def _build_container_runtime_context_block(self) -> dict[str, Any] | None:
        if not self._is_claude_container_file_attach_present_in_current_tools():
            return None

        lines = [
            "Container working-set state:",
            "",
            "Known conversation URLs:",
        ]

        all_turn_urls = list(getattr(self.action_config, "all_turn_urls", None) or [])
        if all_turn_urls:
            for url in all_turn_urls[-20:]:
                lines.append(f"- {self._display_name_from_url(url)}: {url}")
        else:
            lines.append("- none")

        lines.extend(["", "Current turn URLs:"])
        current_turn_urls = list(getattr(self.action_config, "current_turn_urls", None) or [])
        if current_turn_urls:
            for url in current_turn_urls[:10]:
                lines.append(f"- {self._display_name_from_url(url)}: {url}")
        else:
            lines.append("- none")

        lines.extend(["", "Container candidate URLs:"])
        container_candidate_urls = list(getattr(self.action_config, "container_candidate_urls", None) or [])
        if container_candidate_urls:
            for url in container_candidate_urls[:15]:
                lines.append(f"- {self._display_name_from_url(url)}: {url}")
        else:
            lines.append("- none")

        lines.extend(["", "Uploaded Claude file cache:"])
        uploaded_files_manifest = self._to_plain(
            getattr(self.action_config, "uploaded_files_manifest", None) or []
        )
        if uploaded_files_manifest:
            for item in uploaded_files_manifest[-15:]:
                lines.append(
                    f"- source_url={item.get('source_url')} claude_file_id={item.get('claude_file_id')} "
                    f"file_name={item.get('file_name')} last_used_turn={item.get('last_used_turn')}"
                )
        else:
            lines.append("- none")

        lines.extend(["", "Files currently attached to Claude container:"])
        container_available_files = self._to_plain(
            getattr(self.action_config, "container_available_files", None) or []
        )
        if container_available_files:
            for item in container_available_files[-15:]:
                lines.append(
                    f"- container_id={item.get('container_id')} source_url={item.get('source_url')} "
                    f"claude_file_id={item.get('claude_file_id')} attached_turn={item.get('attached_turn')}"
                )
        else:
            lines.append("- none")

        lines.extend(
            [
                "",
                "Rules:",
                "- Use claude_container_file_attach to attach selected known URLs into Claude's current working container.",
                "- Never use temporary file path present in container for tool calling instead always generate file and you will get a s3 url and then use it for tool calling.",
                "- The uploaded Claude file cache is persistent across turns and should be reused before any re-upload.",
                "- Only files listed under 'Files currently attached to Claude container' are guaranteed to be directly available in code execution right now.",
                "- Always use `claude_container_file_attach` to attach excel, csv and tsv files for processing.",
                "- If `openai_vector_store_tool` tool is available than Do not use `openai_vector_store_tool` for csv, tsv, xls, or xlsx files.",
                "- Never pass more than 15 new file URLs to `claude_container_file_attach` in a single tool call.",
                "- Respect current container remaining capacity before attaching more files.",
            ]
        )
        return {"type": "text", "text": "\n".join(lines)}

    def _maybe_enable_interleaved_thinking_beta(self) -> None:
        provider = self._current_provider()
        model = self._current_model()
        thinking_type = self._thinking_type()

        if provider != ClaudeProvider.ANTHROPIC:
            return
        if not self._tools_present_for_request():
            return
        if thinking_type != "enabled":
            return

        if (
            model.startswith("claude-sonnet-4-6")
            or model.startswith("claude-opus-4-5")
            or model.startswith("claude-opus-4-1")
            or model.startswith("claude-opus-4-20250514")
            or model == "claude-opus-4"
            or model.startswith("claude-sonnet-4-5")
            or model.startswith("claude-sonnet-4-20250514")
            or model == "claude-sonnet-4"
        ):
            self._append_beta("interleaved-thinking-2025-05-14")
            logger.info(
                "Enabled interleaved thinking beta model={} provider={}",
                model,
                provider,
            )

    def _resolved_capabilities(self):
        provider = getattr(self.action_config, "provider", ClaudeProvider.ANTHROPIC)
        model = str(getattr(self.action_config, "model", "claude-sonnet-4-6"))
        return ClaudeModelRegistry.resolve(model, provider).capabilities


    def _current_provider(self) -> ClaudeProvider:
        return getattr(self.action_config, "provider", ClaudeProvider.ANTHROPIC)

    def _current_model(self) -> str:
        return str(getattr(self.action_config, "model", "claude-sonnet-4-6"))

    def _normalize_latest_web_tool(
        self,
        tool_def: dict[str, Any],
        *,
        is_fetch: bool,
        preserve_user_allowed_callers: bool = False,
    ) -> dict[str, Any]:
        """
        Make latest web tools safe across models/providers.

        - For non-4.6 models and non-Anthropic providers, force direct-only mode.
        - For 4.6 Anthropic models, keep dynamic filtering path available unless
          caller explicitly pinned direct mode.
        """
        tool_def = dict(tool_def)

        if is_fetch:
            tool_def.setdefault("name", "web_fetch")
            tool_def.setdefault("citations", {"enabled": bool(self.action_config.citations)})
        else:
            tool_def.setdefault("name", "web_search")

        if self._should_force_direct_latest_web_tools():
            existing = tool_def.get("allowed_callers")
            if existing != ["direct"]:
                if existing is not None and not preserve_user_allowed_callers:
                    logger.bind(
                        provider=self._current_provider(),
                        model=self._current_model(),
                        tool_type=tool_def.get("type"),
                    ).warning(
                        "Overriding allowed_callers={} to ['direct'] for compatibility",
                        existing,
                    )
                tool_def["allowed_callers"] = ["direct"]

        return tool_def

    def _web_tool_implies_auto_code_execution(self, tool: Any) -> bool:
        """
        Only treat latest web tools as implying internal code execution when
        dynamic filtering is actually allowed for the current model/provider.
        """
        if self._should_force_direct_latest_web_tools():
            return False

        if isinstance(tool, str):
            return tool in {"web_search_claude", "web_fetch_claude"}

        if isinstance(tool, dict):
            tool_type = tool.get("type")
            if tool_type not in {"web_search_20260209", "web_fetch_20260209"}:
                return False

            allowed_callers = tool.get("allowed_callers")
            if allowed_callers == ["direct"]:
                return False
            return True

        return False
    

    def _recent_generated_file_refs(self) -> list[dict[str, Any]]:
        items = self._to_plain(getattr(self.action_config, "generated_files", None) or [])

        selected: list[dict[str, Any]] = []
        for raw_item in reversed(items):
            if not isinstance(raw_item, dict):
                continue

            selected.append(raw_item)

        selected.reverse()
        return selected

    def _build_generated_files_context_block(self) -> dict[str, Any] | None:
        recent_files = self._recent_generated_file_refs()
        if not recent_files:
            return None

        lines = [
            "Recent generated files from the previous turns may be available in the current working set:",
        ]
        for item in recent_files:
            file_name = item.get("file_name") or item.get("filename") or "unnamed_file"
            s3_url = item.get("s3_url") or item.get("url")
            file_id = item.get("file_id")
            line = f"- {file_name}"
            if file_id:
                line += f" (file_id={file_id})"
            if s3_url:
                line += f" ({s3_url})"
            lines.append(line)

        return {"type": "text", "text": "\n".join(lines)}

    def _extract_generated_urls(self, generated_files: list[Any] | None = None) -> list[str]:
        items = generated_files
        if items is None:
            items = getattr(self.action_config, "generated_files", None) or []

        urls: list[str] = []
        seen: set[str] = set()

        for raw_item in items:
            if hasattr(raw_item, "model_dump"):
                item = raw_item.model_dump(exclude_none=True)
            elif isinstance(raw_item, dict):
                item = dict(raw_item)
            else:
                continue

            s3_url = item.get("s3_url") or item.get("url")
            if not s3_url:
                continue

            s3_url = str(s3_url)
            if s3_url not in seen:
                seen.add(s3_url)
                urls.append(s3_url)

        return urls
    

    @staticmethod
    def _dedupe_preserve_order(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            value = str(item or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out

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

    def _find_manifest_entry(
        self,
        manifest: list[dict[str, Any]],
        *,
        source_url: str | None = None,
        prepared_url: str | None = None,
    ) -> dict[str, Any] | None:
        for item in manifest:
            if source_url and item.get("source_url") == source_url:
                return item
            if prepared_url and item.get("prepared_url") == prepared_url:
                return item
        return None

    
    def _upsert_vector_store_manifest_entry(
        self,
        manifest: list[dict[str, Any]],
        *,
        source_url: str,
        prepared_url: str | None,
        source: str,
        representation: str,
        status: str,
        turn: int,
        display_name: str | None = None,
        last_error: str | None = None,
        source_content_sha256: str | None = None,
        vector_store_file_id: str | None = None,
        openai_file_id: str | None = None,
        last_indexed_turn: int | None = None,
    ) -> list[dict[str, Any]]:
        existing = self._find_manifest_entry(
            manifest,
            source_url=source_url,
            prepared_url=prepared_url,
        )

        payload = {
            "source_url": source_url,
            "prepared_url": prepared_url,
            "display_name": display_name or self._display_name_from_url(source_url),
            "source": source,
            "representation": representation,
            "status": status,
            "last_error": last_error,
            "source_content_sha256": source_content_sha256,
            "vector_store_file_id": vector_store_file_id,
            "openai_file_id": openai_file_id,
            "last_seen_turn": turn,
            "last_indexed_turn": last_indexed_turn,
        }

        if existing:
            existing.update({k: v for k, v in payload.items() if v is not None})
            existing["first_seen_turn"] = min(
                int(existing.get("first_seen_turn") or turn),
                turn,
            )
            return manifest

        manifest.append(
            {
                "first_seen_turn": turn,
                "last_seen_turn": turn,
                **{k: v for k, v in payload.items() if v is not None},
            }
        )
        return manifest

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

    async def _ensure_vector_store_exists(self, ctx: Context, app_state: AppState, notify: Notify) -> None:
        if not getattr(self.action_config, "vector_store_enabled", True):
            return

        if getattr(self.action_config, "vector_store_id", None):
            return

        name = getattr(self.action_config, "vector_store_name", None) or f"claude-chat-{gen_uuid()}"
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
        vector_store = payload.get("vector_store") or {}
        vector_store_id = payload.get("vector_store_id") or vector_store.get("id")

        if not vector_store_id:
            raise HTTPException(status_code=500, detail="Failed to create conversation vector store")

        self.action_config.vector_store_id = vector_store_id
        self.action_config.vector_store_name = vector_store.get("name") or name

    _TOOL_BLOCK_TYPES = {"tool_use", "tool_result"}

    def _history_vector_store_enabled(self) -> bool:
        return bool(getattr(self.action_config, "history_vector_store_enabled", True))

    def _history_index_every_n_turns(self) -> int:
        raw = getattr(self.action_config, "history_vector_store_index_every_n_turns", 20)
        try:
            value = int(raw)
        except Exception:
            value = 20
        return max(1, min(200, value))

    async def _ensure_history_vector_store_exists(self, ctx: Context, app_state: AppState, notify: Notify | None) -> None:
        if getattr(self.action_config, "history_vector_store_id", None):
            return

        name = (
            getattr(self.action_config, "history_vector_store_name", None)
            or f"claude-chat-history-{gen_uuid()}"
        )
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
        vector_store = payload.get("vector_store") or {}
        vector_store_id = payload.get("vector_store_id") or vector_store.get("id")

        if not vector_store_id:
            raise HTTPException(
                status_code=500, detail="Failed to create conversation history vector store"
            )

        self.action_config.history_vector_store_id = vector_store_id
        self.action_config.history_vector_store_name = vector_store.get("name") or name

    def _stringify_history_content_block(self, block: Any) -> str:
        block = self._to_plain(block)
        if not isinstance(block, dict):
            return str(block) if block is not None else ""

        block_type = block.get("type")
        if block_type == "text":
            return str(block.get("text") or "")

        if block_type == "tool_use":
            try:
                tool_input = json.dumps(block.get("input") or {}, ensure_ascii=False, default=str)
            except Exception:
                tool_input = str(block.get("input"))
            return f"[called tool {block.get('name')} with input {tool_input[:500]}]"

        if block_type == "tool_result":
            content = block.get("content")
            if isinstance(content, list):
                parts = [self._stringify_history_content_block(c) for c in content]
                text = " ".join(p for p in parts if p)
            else:
                text = str(content or "")
            return f"[tool result: {text[:1000]}]"

        if block_type in {"image", "document"}:
            return f"[{block_type} attachment]"

        try:
            return json.dumps(block, ensure_ascii=False, default=str)[:500]
        except Exception:
            return str(block)[:500]

    def _stringify_history_message_content(self, content: Any, *, tool_blocks_only: bool) -> str:
        if isinstance(content, str):
            return "" if tool_blocks_only else content
        if isinstance(content, list):
            parts = []
            for block in content:
                plain = self._to_plain(block)
                block_type = plain.get("type") if isinstance(plain, dict) else None
                if (block_type in self._TOOL_BLOCK_TYPES) != tool_blocks_only:
                    continue
                part = self._stringify_history_content_block(block)
                if part:
                    parts.append(part)
            return "\n".join(parts)
        return ""

    def _collect_turn_messages(
        self, *, turns: list[dict[str, Any]], tool_blocks_only: bool
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        for raw_turn in turns:
            turn = self._to_plain(raw_turn)
            if not isinstance(turn, dict):
                continue

            messages: list[dict[str, str]] = []
            for raw_msg in turn.get("messages") or []:
                msg = self._to_plain(raw_msg)
                if not isinstance(msg, dict):
                    continue
                text = self._stringify_history_message_content(
                    msg.get("content"), tool_blocks_only=tool_blocks_only
                )
                if text.strip():
                    messages.append({"role": msg.get("role") or "unknown", "content": text.strip()})

            if messages:
                collected.append({"turn": turn.get("turn"), "messages": messages})

        return collected

    def _render_turn_history_messages_as_json(self, *, turns: list[dict[str, Any]]) -> str:
        collected = self._collect_turn_messages(turns=turns, tool_blocks_only=False)
        return json.dumps(collected, ensure_ascii=False, default=str)

    def _render_turn_history_tool_activity_as_text(self, *, turns: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for turn in self._collect_turn_messages(turns=turns, tool_blocks_only=True):
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
        app_state: AppState,
        notify: Notify | None,
        *,
        turn_start: int,
        turn_end: int,
        content_types: set[str],
    ) -> None:
        """Replaces a stale duplicate from a retried index; never touches other turn ranges."""
        list_result = await openai_vector_store_tool(
            config=OpenAIVectorStoreToolConfig(
                operation=OpenAIVectorStoreToolOperation.LIST_VECTOR_STORE_FILES,
                vector_store_id=self.action_config.history_vector_store_id,
            ),
            ctx=ctx,
            app_state=app_state,
            user=self.user,
            db=self.db,
            notify=notify,
        )
        payload = self._extract_json_payload_from_datablocks(list_result)

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

        # Only detaches from this vector store; openai_vector_store_tool always keeps
        # the underlying OpenAI file (it may be shared by other stores).
        await openai_vector_store_tool(
            config=OpenAIVectorStoreToolConfig(
                operation=OpenAIVectorStoreToolOperation.DELETE_VECTOR_STORE_FILES,
                vector_store_id=self.action_config.history_vector_store_id,
                delete_file_ids=stale_ids,
            ),
            ctx=ctx,
            app_state=app_state,
            user=self.user,
            db=self.db,
            notify=notify,
        )
        logger.bind(
            ctx=ctx, turn_start=turn_start, turn_end=turn_end, stale_file_count=len(stale_ids)
        ).info("Removed stale vector-store files before re-indexing this turn range")

    async def _index_turn_history_batch_to_vector_store(
        self,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None,
        *,
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
            app_state,
            notify,
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
                vector_store_id=self.action_config.history_vector_store_id,
                files=files_to_attach,
                wait_for_completion=True,
            ),
            ctx=ctx,
            app_state=app_state,
            user=self.user,
            db=self.db,
            notify=notify,
        )
        payload = self._extract_json_payload_from_datablocks(result)
        if not payload.get("ok", False):
            logger.bind(ctx=ctx, turn_start=turn_start, turn_end=turn_end).warning(
                "Conversation history vector-store indexing reported failure: {}", payload
            )

    async def _maybe_index_turn_history_to_vector_store(
        self,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None,
        *,
        current_turn: int,
    ) -> None:
        """Best-effort: failures here must never break the chat turn."""
        if not self._history_vector_store_enabled():
            return

        every_n = self._history_index_every_n_turns()
        if current_turn <= 0 or current_turn % every_n != 0:
            return

        last_indexed = int(getattr(self.action_config, "history_vector_store_last_indexed_turn", 0) or 0)
        if current_turn <= last_indexed:
            return

        try:
            await self._ensure_history_vector_store_exists(ctx=ctx, app_state=app_state, notify=notify)

            history = self._to_plain(getattr(self.action_config, "turn_history", None) or [])
            batch = [
                t
                for t in history
                if isinstance(t, dict) and last_indexed < int(t.get("turn") or 0) <= current_turn
            ]
            if not batch:
                self.action_config.history_vector_store_last_indexed_turn = current_turn
                return

            await self._index_turn_history_batch_to_vector_store(
                ctx=ctx,
                app_state=app_state,
                notify=notify,
                turns=batch,
                turn_start=last_indexed + 1,
                turn_end=current_turn,
            )
            self.action_config.history_vector_store_last_indexed_turn = current_turn
            logger.bind(
                ctx=ctx,
                turn_start=last_indexed + 1,
                turn_end=current_turn,
                history_vector_store_id=getattr(self.action_config, "history_vector_store_id", None),
            ).info("Indexed conversation turn-history batch into vector store")
        except Exception as exc:
            logger.bind(ctx=ctx, current_turn=current_turn).warning(
                "Failed indexing conversation turn-history batch into vector store: {}", exc
            )

    def _select_recent_generated_urls_for_working_set(self) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()

        for item in reversed(self._recent_generated_file_refs()):
            url = item.get("s3_url") or item.get("url")
            if not url:
                continue
            url = str(url)
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)

        urls.reverse()
        return urls

    def _stamp_generated_files_for_turn(
        self,
        files: list[Any] | None,
        *,
        turn: int,
    ) -> list[dict[str, Any]]:
        stamped: list[dict[str, Any]] = []
        for raw_item in files or []:
            if hasattr(raw_item, "model_dump"):
                item = raw_item.model_dump(exclude_none=True)
            elif isinstance(raw_item, dict):
                item = dict(raw_item)
            else:
                item = self._to_plain(raw_item)
                if not isinstance(item, dict):
                    continue

            item.setdefault("created_turn", turn)
            item["last_seen_turn"] = turn
            stamped.append(item)

        return stamped

    async def _prepare_container_runtime_state(
        self,
        ctx: Context,
        action_input: DataBlock,
    ) -> None:
        action_input.get_llm_prompt(ctx=ctx) # this will propogate action_input.urls due to process_in_text = true
        current_turn_urls = self._dedupe_preserve_order(list(action_input.urls or []))
        all_turn_urls = self._dedupe_preserve_order(
            list(getattr(self.action_config, "all_turn_urls", None) or []) + current_turn_urls
        )

        recent_generated_urls = self._select_recent_generated_urls_for_working_set()
        container_candidate_urls = self._dedupe_preserve_order(
            current_turn_urls + recent_generated_urls
        )

        self.action_config.current_turn_urls = current_turn_urls
        self.action_config.all_turn_urls = all_turn_urls
        self.action_config.container_candidate_urls = container_candidate_urls
        self.action_config.current_turn_container_urls = []

    async def _prepare_vector_store_state(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
    ) -> None:
        turn = int(getattr(self.action_config, "chat_turn", 1) or 1)
        current_turn_urls = self._dedupe_preserve_order(list(action_input.urls or []))

        manifest = self._to_plain(getattr(self.action_config, "vector_store_manifest", None) or [])
        current_turn_vector_store_urls: list[str] = []

        for source_url in current_turn_urls:
            existing = self._find_manifest_entry(manifest, source_url=source_url)

            current_source_sha256 = await self._compute_source_url_sha256(
                ctx=ctx,
                source_url=source_url,
            )
            existing_source_sha256 = str(existing.get("source_content_sha256") or "").strip() if existing else ""

            content_changed = bool(
                existing
                and current_source_sha256
                and existing_source_sha256 != current_source_sha256
            )

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

            manifest = self._upsert_vector_store_manifest_entry(
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

        self.action_config.current_turn_vector_store_urls = self._dedupe_preserve_order(
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
        manifest = self._to_plain(getattr(self.action_config, "vector_store_manifest", None) or [])

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

            vector_store_file_id = str(
                indexed_entry.get("vector_store_file_id") or ""
            ).strip() or None

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

            manifest = self._upsert_vector_store_manifest_entry(
                manifest,
                source_url=source_url,
                prepared_url=prepared_url,
                source="user_input",
                representation=(
                    "native_vector_store"
                    if prepared_url == source_url
                    else "converted_txt_vector_store"
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

        vector_store_id = getattr(self.action_config, "vector_store_id", None)
        if not vector_store_id:
            return

        manifest = self._to_plain(getattr(self.action_config, "vector_store_manifest", None) or [])
        turn = int(getattr(self.action_config, "chat_turn", 1) or 1)

        files_to_add: list[OpenAIVectorStoreFileInput] = []
        prepared_to_source_url: dict[str, str] = {}
        delete_file_ids: list[str] = []

        for source_url in list(getattr(self.action_config, "current_turn_urls", None) or []):
            entry = self._find_manifest_entry(manifest, source_url=source_url)
            if not entry:
                continue

            prepared_url = entry.get("prepared_url")
            status = str(entry.get("status") or "")
            source_content_sha256 = entry.get("source_content_sha256")
            stale_vector_store_file_id = str(
                entry.get("vector_store_file_id") or ""
            ).strip()

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

        delete_file_ids = self._dedupe_preserve_order(delete_file_ids)

        operation = (
            OpenAIVectorStoreToolOperation.REINDEX_FILES
            if delete_file_ids
            else OpenAIVectorStoreToolOperation.ADD_FILES
        )

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

    def _build_vector_store_context_block(self) -> dict[str, Any] | None:
        if not getattr(self.action_config, "vector_store_enabled", True):
            return None

        if not self._is_openai_vector_store_tool_present_in_current_tools():
            return None

        manifest = self._to_plain(getattr(self.action_config, "vector_store_manifest", None) or [])
        indexed_count = sum(1 for item in manifest if item.get("status") == "indexed")
        prepared_count = sum(1 for item in manifest if item.get("status") == "prepared")
        pending_count = sum(1 for item in manifest if item.get("status") == "pending")
        failed_count = sum(1 for item in manifest if item.get("status") in {"failed", "unsupported"})

        lines = [
            "Persistent retrieval state:",
            f"- vector_store_enabled: {bool(getattr(self.action_config, 'vector_store_enabled', True))}",
            f"- vector_store_id: {getattr(self.action_config, 'vector_store_id', None)}",
            f"- indexed_files_count: {indexed_count}",
            f"- prepared_files_count: {prepared_count}",
            f"- pending_files_count: {pending_count}",
            f"- failed_files_count: {failed_count}",
            "",
            "Vector-store-ready URLs for this turn:",
        ]

        current_turn_vector_store_urls = list(
            getattr(self.action_config, "current_turn_vector_store_urls", None) or []
        )
        if current_turn_vector_store_urls:
            for url in current_turn_vector_store_urls[:10]:
                lines.append(f"- {self._display_name_from_url(url)}: {url}")
        else:
            lines.append("- none")
        lines.extend(
            [
                "",
                "Rules:",
                "- Current turn URLs are automatically prepared and indexed into the vector store when possible.",
                "- Use openai_vector_store_tool to query indexed files or add new URLs discovered mid-conversation.",
                "- Do not use openai_vector_store_tool for csv, tsv, xls, or xlsx files.",
                "- If this openai_vector_store_tool tool fails sometime and if code_execution and claude_container_file_attach both is available than you can attach files to container using this tool claude_container_file_attach and proceed further.",
            ]
        )
        return {"type": "text", "text": "\n".join(lines)}

    def _merge_generated_files_history(
        self,
        previous_files: list[Any] | None,
        current_turn_files: list[Any] | None,
    ) -> list[dict[str, Any]]:
        merged_by_identity: dict[str, dict[str, Any]] = {}
        order: list[str] = []

        for raw_item in (previous_files or []) + (current_turn_files or []):
            if hasattr(raw_item, "model_dump"):
                item = raw_item.model_dump(exclude_none=True)
            elif isinstance(raw_item, dict):
                item = dict(raw_item)
            else:
                item = self._to_plain(raw_item)
                if not isinstance(item, dict):
                    continue

            item = self._to_plain(item)

            identity = str(
                item.get("file_id")
                or item.get("s3_url")
                or item.get("url")
                or item.get("file_name")
                or item.get("filename")
            )
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
                        existing[key] = min(
                            int(existing.get(key) or value),
                            int(value),
                        )
                    except Exception:
                        existing[key] = existing.get(key) or value
                    continue

                if key == "last_seen_turn":
                    try:
                        existing[key] = max(
                            int(existing.get(key) or value),
                            int(value),
                        )
                    except Exception:
                        existing[key] = value
                    continue

                existing[key] = value

        return [merged_by_identity[item_id] for item_id in order]
    

    def _strip_generated_file_urls_from_text(self, text: str) -> str:
        text = re.sub(
            r'https?://[^\s"\']*amazonaws\.com/files/[^\s"\']+',
            "",
            text,
        )
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    
    def _to_plain(self, obj):
        if hasattr(obj, "model_dump"):
            try:
                obj = obj.model_dump(exclude_none=True)
            except Exception:
                pass

        if hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes, dict, list)):
            try:
                obj = list(obj)
            except Exception:
                return str(obj)
        if isinstance(obj, list):
            return [self._to_plain(x) for x in obj]

        if isinstance(obj, dict):
            return {k: self._to_plain(v) for k, v in obj.items()}
        return obj
    
    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return ClaudeChatConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return ClaudeChatConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return ClaudeChatConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    async def update_config_with_prev_results(
        ctx: Context,
        app_state: AppState,
        curr_config: ClaudeChatConfig,
        prev_results: List[ActionResult] | None = None,
    ) -> ClaudeChatConfig:
        return curr_config

    def _merge_tool_results_history(
        self,
        previous_items: list[Any] | None,
        current_items: list[Any] | None,
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()

        for raw_item in (previous_items or []) + (current_items or []):
            if hasattr(raw_item, "model_dump"):
                item = raw_item.model_dump(exclude_none=True)
            elif isinstance(raw_item, dict):
                item = dict(raw_item)
            else:
                item = self._to_plain(raw_item)
                if not isinstance(item, dict):
                    continue

            item = self._to_plain(item)
            identity = str(item.get("tool_use_id") or "")
            if not identity or identity in seen:
                continue

            seen.add(identity)
            merged.append(item)

        return merged
    

    @staticmethod
    def _is_runtime_context_text_block(text: str) -> bool:
        value = str(text or "").strip()
        return (
            value.startswith("Persistent retrieval state:")
            or value.startswith("Container working-set state:")
            or value.startswith("Recent generated files from the previous turns may be available in the current working set:")
        )

    def _strip_runtime_context_blocks_from_persisted_messages(self) -> None:
        cleaned_messages: list[dict[str, Any]] = []

        for raw_message in self.action_config.messages or []:
            message = self._to_plain(raw_message)
            if not isinstance(message, dict):
                continue

            if message.get("role") != "user":
                cleaned_messages.append(message)
                continue

            content = message.get("content")
            if not isinstance(content, list):
                cleaned_messages.append(message)
                continue

            cleaned_content: list[Any] = []
            for raw_block in content:
                block = self._to_plain(raw_block)

                if not isinstance(block, dict):
                    cleaned_content.append(block)
                    continue

                if block.get("type") == "text" and self._is_runtime_context_text_block(
                    block.get("text", "")
                ):
                    continue

                cleaned_content.append(block)

            message["content"] = cleaned_content
            cleaned_messages.append(message)

        self.action_config.messages = cleaned_messages

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        data_blocks = DataBlocks()

        def _summarize_generated_files(items: list[Any] | None) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for raw in (items or [])[-10:]:
                if hasattr(raw, "model_dump"):
                    item = raw.model_dump(exclude_none=True)
                elif isinstance(raw, dict):
                    item = dict(raw)
                else:
                    item = self._to_plain(raw)
                    if not isinstance(item, dict):
                        continue

                out.append(
                    {
                        "file_id": item.get("file_id"),
                        "file_name": item.get("file_name") or item.get("filename"),
                        "s3_url": item.get("s3_url") or item.get("url"),
                    }
                )
            return out

        def _summarize_config(cfg: Any) -> dict[str, Any]:
            plain = self._to_plain(cfg)
            if not isinstance(plain, dict):
                return {"raw_type": type(cfg).__name__}

            messages = plain.get("messages") or []
            tools = plain.get("tools") or []
            betas = plain.get("betas") or []
            generated_files = plain.get("generated_files") or []
            mcp_servers = plain.get("mcp_servers") or []
            tool_results = plain.get("tool_results") or []
            tool_results_history = plain.get("tool_results_history") or []

            return {
                "model": plain.get("model"),
                "provider": plain.get("provider"),
                "stream": plain.get("stream"),
                "citations": plain.get("citations"),
                "computer_use": plain.get("computer_use"),
                "tool_count": len(tools),
                "tools": tools,
                "betas": betas,
                "mcp_server_count": len(mcp_servers),
                "message_count": len(messages),
                "last_message_role": messages[-1].get("role") if messages else None,
                "generated_files_count": len(generated_files),
                "generated_files": _summarize_generated_files(generated_files),
                "tool_results_count": len(tool_results),
                "tool_results_history_count": len(tool_results_history),
                "vector_store_id": plain.get("vector_store_id"),
                "all_turn_urls_count": len(plain.get("all_turn_urls") or []),
                "current_turn_urls_count": len(plain.get("current_turn_urls") or []),
                "container_candidate_urls_count": len(plain.get("container_candidate_urls") or []),
                "vector_store_manifest_count": len(plain.get("vector_store_manifest") or []),
                "uploaded_files_manifest_count": len(plain.get("uploaded_files_manifest") or []),
                "container_available_files_count": len(plain.get("container_available_files") or []),
            }

        def _log_config_state(stage: str, cfg: Any) -> None:
            logger.bind(ctx=ctx, stage=stage).info(
                "Claude config snapshot: {}",
                _summarize_config(cfg),
            )

        turn_start_index = -1
        try:
            logger.bind(
                ctx=ctx,
                action_input_text_len=len(action_input.text or ""),
                action_input_urls_count=len(action_input.urls or []),
                action_input_tools_count=len(action_input.tools or []),
            ).info("Starting Claude run_action")
            if not getattr(self.action_config, "chat_turn", None):
                self.action_config.chat_turn = 1

            # Bounded conversation context: rebuild working messages from persisted history.
            self._ensure_pinned_messages_initialized()
            self._rebuild_working_messages_from_history()
            self._prune_empty_user_messages()
            turn_start_index = len(self.action_config.messages or [])
            if app_state.cancellation is not None:
                app_state.cancellation.set_snapshot_meta(
                    turn_start_index=turn_start_index,
                    chat_turn=int(getattr(self.action_config, "chat_turn", 1) or 1),
                )

            _log_config_state("initial_action_config", self.action_config)

            client = await AppConn.get_claude(
                ctx=ctx, db=self.db, user=self.user,
                provider=getattr(self.action_config, "provider", ClaudeProvider.ANTHROPIC),
            )
            if isinstance(client, Exception):
                raise client

            previous_generated_files = self._to_plain(
                getattr(self.action_config, "generated_files", None) or []
            )
            logger.bind(ctx=ctx).info(f"Loaded previous generated files from action_config - previous_generated_files_count={len(previous_generated_files)} \n\n previous_generated_files={_summarize_generated_files(previous_generated_files)}")

            previous_tool_results = self._to_plain(
                getattr(self.action_config, "tool_results", None) or []
            )
            logger.bind(
                ctx=ctx,
                previous_tool_results_count=len(previous_tool_results),
            ).info("Loaded previous tool results from action_config")

            runtime_tool_defs = await self.replace_tools_with_tools_defs(
                action_input=action_input,
                for_persistence=False,
            )

            persisted_tool_defs = await self.replace_tools_with_tools_defs(
                action_input=action_input,
                for_persistence=True,
            )

            logger.bind(
                ctx=ctx,
                runtime_tool_defs_count=len(runtime_tool_defs or []),
                persisted_tool_defs_count=len(persisted_tool_defs or []),
            ).info("Resolved Claude tool definitions")

            # Save resolved tools in DB/config.
            # This includes code_execution when code_interpreter was configured.
            self.action_config.tools = self._to_plain(persisted_tool_defs)

            _log_config_state("after_replace_tools", self.action_config)

            await self._prepare_container_runtime_state(ctx=ctx, action_input=action_input)
            _log_config_state("after_prepare_container_runtime_state", self.action_config)

            vector_store_active = self._is_openai_vector_store_tool_requested()

            if vector_store_active:
                await self._ensure_vector_store_exists(ctx=ctx, app_state=app_state, notify=notify)
                await self._prepare_vector_store_state(ctx=ctx, app_state=app_state, action_input=action_input)
                _log_config_state("after_prepare_vector_store_state", self.action_config)

                await self._auto_index_current_turn_urls_to_vector_store(ctx=ctx, app_state=app_state, notify=notify)
                _log_config_state("after_auto_index_current_turn_urls_to_vector_store", self.action_config)
            else:
                self.action_config.current_turn_vector_store_urls = []
                logger.bind(ctx=ctx).info(
                    "Skipping vector store activation because openai_vector_store_tool was not explicitly requested"
                )

            await self.preprocess_input_messages(
                ctx=ctx,
                action_input=action_input,
                claude_client=client,
            )
            _log_config_state("after_preprocess_input_messages", self.action_config)

            await self.process_computer_use_config(ctx=ctx, client=client)
            _log_config_state("after_process_computer_use_config", self.action_config)

            await self.add_mcp_servers(ctx=ctx)
            self._maybe_enable_interleaved_thinking_beta()
            _log_config_state("after_maybe_enable_interleaved_thinking_beta", self.action_config)

            chat_req_payload = self.action_config.model_dump(exclude_none=True)
            logger.bind(
                ctx=ctx,
                chat_req_payload_before_reset_generated_files=_summarize_config(chat_req_payload),
            ).info("Built chat_req_payload from action_config")

            # Runtime request must use the web-tool-safe runtime list,
            # not the persisted config.tools list.
            runtime_tools = self._to_plain(runtime_tool_defs) or []
            # `runtime_tool_defs` was resolved before `add_mcp_servers`, so it may still
            # hold the raw `mcp_<server>` string instead of the resolved `mcp_toolset`
            # dict. Merge in any `mcp_toolset` dicts that `add_mcp_servers` produced so
            # the runtime request actually activates MCP on the first turn.
            existing_toolset_servers = {
                t.get("mcp_server_name")
                for t in runtime_tools
                if isinstance(t, dict) and t.get("type") == "mcp_toolset"
            }
            for tool in (self.action_config.tools or []):
                if (
                    isinstance(tool, dict)
                    and tool.get("type") == "mcp_toolset"
                    and tool.get("mcp_server_name") not in existing_toolset_servers
                ):
                    runtime_tools.append(tool)
                    existing_toolset_servers.add(tool.get("mcp_server_name"))
            chat_req_payload["tools"] = runtime_tools

            self._strip_runtime_context_blocks_from_persisted_messages()
            self.action_config = ClaudeChatConfig.model_validate(self._to_plain(self.action_config))
            chat_req_payload["generated_files"] = []
            logger.bind(
                ctx=ctx,
                chat_req_payload_after_reset_generated_files=_summarize_config(chat_req_payload),
            ).info("Reset chat_req_payload.generated_files for current run")
            chat_req_payload["tool_results"] = []
            chat_req_payload["tool_results_history"] = previous_tool_results

            chat_req = ChatReqClaude(**chat_req_payload)

            logger.bind(
                ctx=ctx,
                model=chat_req.model,
                provider=chat_req.provider,
                betas=chat_req.betas,
                tools_count=len(chat_req.tools or []),
                chat_req_summary=_summarize_config(chat_req),
            ).info("Prepared Claude chat request")

            chat_res = await client.claude_chat.chat(
                user=self.user,
                ctx=ctx,
                app_state=app_state,
                chat_req=chat_req,
                db=self.db,
                notify=notify,
                tool_output_text_to_file_char_limit=self.action_config.tool_output_text_to_file_char_limit,
            )

            if getattr(chat_req, "container", None):
                self.action_config.container = chat_req.container

            logger.bind(
                ctx=ctx,
                chat_res_exists=bool(chat_res),
                chat_res_stop_reason=getattr(chat_res, "stop_reason", None) if chat_res else None,
                chat_res_content_count=len(chat_res.content) if chat_res and getattr(chat_res, "content", None) else 0,
            ).info("Claude chat returned")

            current_turn_generated_files = self._stamp_generated_files_for_turn(
                self._to_plain(getattr(chat_req, "generated_files", None) or []),
                turn=int(getattr(self.action_config, "chat_turn", 1) or 1),
            )
            logger.bind(
                ctx=ctx,
                current_turn_generated_files_count=len(current_turn_generated_files),
                current_turn_generated_files=_summarize_generated_files(current_turn_generated_files),
            ).info("Collected current turn generated files from chat_req")

            current_turn_tool_results = self._to_plain(
                getattr(chat_req, "tool_results", None) or []
            )
            logger.bind(
                ctx=ctx,
                current_turn_tool_results_count=len(current_turn_tool_results),
            ).info("Collected current turn tool results from chat_req")

            merged_generated_files = self._merge_generated_files_history(
                previous_generated_files,
                current_turn_generated_files,
            )
            logger.bind(
                ctx=ctx,
                merged_generated_files_count=len(merged_generated_files),
                merged_generated_files=_summarize_generated_files(merged_generated_files),
            ).info("Merged generated files history")

            self.action_config.generated_files = merged_generated_files
            _log_config_state("after_persist_generated_files", self.action_config)

            merged_tool_results = self._merge_tool_results_history(
                previous_tool_results,
                current_turn_tool_results,
            )
            logger.bind(
                ctx=ctx,
                merged_tool_results_count=len(merged_tool_results),
            ).info("Merged tool results history")

            self.action_config.tool_results = merged_tool_results
            _log_config_state("after_persist_tool_results", self.action_config)

            self.action_config.vector_store_id = getattr(chat_req, "vector_store_id", None)
            self.action_config.vector_store_name = getattr(chat_req, "vector_store_name", None)
            self.action_config.current_turn_urls = self._to_plain(
                getattr(chat_req, "current_turn_urls", None) or []
            )
            self.action_config.current_turn_vector_store_urls = self._to_plain(
                getattr(chat_req, "current_turn_vector_store_urls", None) or []
            )
            self.action_config.current_turn_container_urls = self._to_plain(
                getattr(chat_req, "current_turn_container_urls", None) or []
            )
            self.action_config.all_turn_urls = self._to_plain(
                getattr(chat_req, "all_turn_urls", None) or []
            )
            self.action_config.vector_store_manifest = self._to_plain(
                getattr(chat_req, "vector_store_manifest", None) or []
            )
            _log_config_state("after_persist_vector_store_state", self.action_config)

            self.action_config.uploaded_files_manifest = self._to_plain(
                getattr(chat_req, "uploaded_files_manifest", None) or []
            )

            self.action_config.container_candidate_urls = self._to_plain(
                getattr(chat_req, "container_candidate_urls", None) or []
            )
            self.action_config.container_available_files = self._to_plain(
                getattr(chat_req, "container_available_files", None) or []
            )

            # Sync back MCP state. `chat_req` is a separate object from `action_config`,
            # so any MCP server/toolset added mid-run via `enable_mcp` (or normalized
            # during request construction) must be carried back to persist across turns.
            # `mcp_servers` and `betas` are synced fully (they are not subject to the
            # runtime/persisted tool stripping). `tools` is merged only for `mcp_toolset`
            # dicts because the runtime tool list may have stripped tools (e.g.
            # code_execution) that must remain in the persisted config.
            # Preserve `None` (absent) instead of converting to `[]`: the
            # model_validator only adds the MCP beta when `mcp_servers` is
            # truthy, so an empty list would reach the API without the beta
            # and be rejected as "Extra inputs are not permitted".
            self.action_config.mcp_servers = (
                self._to_plain(chat_req.mcp_servers) if chat_req.mcp_servers else None
            )
            self.action_config.betas = list(getattr(chat_req, "betas", None) or [])
            existing_toolset_servers = {
                t.get("mcp_server_name")
                for t in (self.action_config.tools or [])
                if isinstance(t, dict) and t.get("type") == "mcp_toolset"
            }
            for tool in (getattr(chat_req, "tools", None) or []):
                if (
                    isinstance(tool, dict)
                    and tool.get("type") == "mcp_toolset"
                    and tool.get("mcp_server_name") not in existing_toolset_servers
                ):
                    if self.action_config.tools is None:
                        self.action_config.tools = []
                    self.action_config.tools.append(self._to_plain(tool))
                    existing_toolset_servers.add(tool.get("mcp_server_name"))

            if not chat_res:
                # Even when Claude returns no response, persist this run as a turn so we don't lose
                # the user's prompt / system-like blocks that were appended during preprocessing.
                try:
                    current_turn = int(getattr(self.action_config, "chat_turn", 1) or 1)

                    delta_has_user = self._delta_has_role(
                        start_index=turn_start_index,
                        role="user",
                    )
                    delta_has_assistant = self._delta_has_role(
                        start_index=turn_start_index,
                        role="assistant",
                    )
                    if delta_has_user and not delta_has_assistant:
                        self._append_synthetic_assistant_failure_message(
                            kind="empty_response",
                        )

                    appended = self._append_current_turn_to_history(
                        turn=current_turn,
                        start_index=turn_start_index,
                    )
                    self._minimize_persisted_messages()
                    if appended:
                        self.action_config.chat_turn = current_turn + 1
                        logger.bind(ctx=ctx, current_turn=current_turn).info(
                            "Persisted partial Claude turn history after empty response"
                        )
                except Exception as exc:
                    logger.bind(ctx=ctx).warning(
                        "Failed persisting partial Claude turn history after empty response: {}",
                        exc,
                    )

                self.action_config = ClaudeChatConfig.model_validate(
                    self._to_plain(self.action_config)
                )
                _log_config_state("validated_action_config_no_chat_res", self.action_config)
                return data_blocks

            generated_urls = self._extract_generated_urls(current_turn_generated_files)
            logger.bind(
                ctx=ctx,
                generated_urls_count=len(generated_urls),
                generated_urls=generated_urls,
            ).info("Extracted generated URLs from current turn generated files")

            self.action_config.all_turn_urls = self._dedupe_preserve_order(
                list(getattr(self.action_config, "all_turn_urls", None) or []) + generated_urls
            )

            data_block_text = ""
            text_blocks_seen = 0
            non_text_blocks_seen: list[str] = []

            for claude_block in chat_res.content:
                match claude_block:
                    case TextBlock():
                        text_blocks_seen += 1
                        data_block_text += f"{claude_block.text}\n"
                    case BetaTextBlock():
                        text_blocks_seen += 1
                        data_block_text += f"{claude_block.text}\n"
                    case _:
                        non_text_blocks_seen.append(type(claude_block).__name__)
                        continue

            logger.bind(
                ctx=ctx,
                text_blocks_seen=text_blocks_seen,
                non_text_blocks_seen=non_text_blocks_seen,
                raw_data_block_text_len=len(data_block_text),
                raw_data_block_text_preview=data_block_text[:50],
            ).info("Built data_block_text from chat_res.content")

            data_block_text = re.sub(
                r"^.*?/files/(output|input)/[a-f0-9]+/\S+.*$",
                "",
                data_block_text,
                flags=re.MULTILINE,
            )

            logger.bind(
                ctx=ctx,
                cleaned_data_block_text_len=len(data_block_text),
                cleaned_data_block_text_preview=data_block_text[:50],
            ).info("Cleaned data_block_text after internal file URL stripping regex")

            generated_file_lines: list[str] = []
            for item in current_turn_generated_files:
                if hasattr(item, "model_dump"):
                    item = item.model_dump(exclude_none=True)
                elif not isinstance(item, dict):
                    continue

                file_name = item.get("file_name") or item.get("filename") or "generated_file"
                file_url = item.get("s3_url") or item.get("url")
                if file_url:
                    generated_file_lines.append(f"{file_name} : {file_url}")

            if generated_file_lines:
                data_block_text = (
                    data_block_text.rstrip()
                    + "\n\nDownload generated files -\n"
                    + "\n".join(generated_file_lines)
                    + "\n"
                )
                
            # Persist assistant turn so next run is user -> assistant -> user
            if data_block_text:
                self.action_config.messages.append(
                    ClaudeMessageParam(
                        role="assistant",
                        content=[{"type": "text", "text": data_block_text}],
                    )
                )
                logger.bind(
                    ctx=ctx,
                    appended_assistant_message_len=len(data_block_text),
                    new_message_count=len(self.action_config.messages or []),
                ).info("Persisted assistant text message into action_config.messages")

            # Persist verbatim per-turn chat history (outside the model context by default).
            try:
                current_turn = int(getattr(self.action_config, "chat_turn", 1) or 1)
                self._append_current_turn_to_history(
                    turn=current_turn,
                    start_index=turn_start_index,
                )
                self._minimize_persisted_messages()
                logger.bind(
                    ctx=ctx,
                    current_turn=current_turn,
                    turn_history_count=len(getattr(self.action_config, "turn_history", None) or []),
                ).info("Persisted Claude turn history and cleared persisted working messages")
            except Exception as exc:
                logger.bind(ctx=ctx).warning("Failed persisting Claude turn history: {}", exc)

            await self._maybe_index_turn_history_to_vector_store(
                ctx=ctx,
                app_state=app_state,
                notify=notify,
                current_turn=int(getattr(self.action_config, "chat_turn", 1) or 1),
            )

            data_blocks.blocks.append(
                DataBlock(
                    text=data_block_text,
                    urls=generated_urls,
                )
            )
            logger.bind(
                ctx=ctx,
                output_blocks_count=len(data_blocks.blocks),
                output_urls_count=len(generated_urls),
            ).info("Constructed output DataBlock")

            self.action_config.chat_turn = int(getattr(self.action_config, "chat_turn", 1)) + 1
            logger.bind(
                ctx=ctx,
                next_chat_turn=self.action_config.chat_turn,
            ).info("Incremented Claude chat_turn after completed run")

            self.action_config = ClaudeChatConfig.model_validate(
                self._to_plain(self.action_config)
            )
            _log_config_state("final_validated_action_config", self.action_config)

            if not data_blocks.blocks:
                raise HTTPException(status_code=503, detail="No response from Claude")

            logger.bind(ctx=ctx).info("Completed Claude run_action successfully")
            return data_blocks

        except Exception as exc:
            logger.bind(ctx=ctx).exception("Error in Claude run_action: {}", exc)

            # Persist this run as a turn even on failure, so we don't lose the user's prompt.
            try:
                if turn_start_index >= 0:
                    current_turn = int(getattr(self.action_config, "chat_turn", 1) or 1)

                    delta_has_user = self._delta_has_role(
                        start_index=turn_start_index,
                        role="user",
                    )
                    delta_has_assistant = self._delta_has_role(
                        start_index=turn_start_index,
                        role="assistant",
                    )
                    if delta_has_user and not delta_has_assistant:
                        self._append_synthetic_assistant_failure_message(
                            kind="error",
                        )

                    appended = self._append_current_turn_to_history(
                        turn=current_turn,
                        start_index=turn_start_index,
                    )
                    self._minimize_persisted_messages()
                    if appended:
                        self.action_config.chat_turn = current_turn + 1
                        logger.bind(ctx=ctx, current_turn=current_turn).info(
                            "Persisted partial Claude turn history after error"
                        )
            except Exception as history_exc:
                logger.bind(ctx=ctx).warning(
                    "Failed persisting partial Claude turn history after error: {}",
                    history_exc,
                )

            try:
                self.action_config = ClaudeChatConfig.model_validate(
                    self._to_plain(self.action_config)
                )
                _log_config_state("validated_action_config_after_error", self.action_config)
            except Exception as validate_exc:
                logger.bind(ctx=ctx).warning(
                    "Failed validating action_config after error: {}",
                    validate_exc,
                )

            return exc
        
    async def add_mcp_servers(self, ctx: Context) -> None:
        # MCP connector (beta: mcp-client-2025-11-20). Tool configuration lives in the
        # `tools` array as `mcp_toolset` objects, not in the server definition. The
        # deprecated `tool_configuration` field on the server def is no longer used.
        sanitised_mcp_servers: list[dict[str, Any]] = []
        seen_server_names: set[str] = set()
        sanitised_tools: list[Any] = []
        seen_toolset_servers: set[str] = set()

        def _add_server(name: str, url: str, authorization_token: str | None) -> None:
            if name in seen_server_names:
                return
            seen_server_names.add(name)
            sanitised_mcp_servers.append(
                {
                    "type": "url",
                    "url": url,
                    "name": name,
                    "authorization_token": authorization_token,
                }
            )

        def _add_toolset(server_name: str) -> None:
            if server_name in seen_toolset_servers:
                return
            seen_toolset_servers.add(server_name)
            sanitised_tools.append(
                {"type": "mcp_toolset", "mcp_server_name": server_name}
            )

        for mcp_server in self.action_config.mcp_servers or []:
            try:
                raw = (
                    mcp_server
                    if isinstance(mcp_server, dict)
                    else (
                        mcp_server.model_dump(exclude_none=True)
                        if hasattr(mcp_server, "model_dump")
                        else {}
                    )
                )
                name = raw.get("name")
                url = raw.get("url")
                if not name or not url:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid MCP server definition: {mcp_server}",
                    )
                _add_server(
                    name=name,
                    url=url,
                    authorization_token=raw.get("authorization_token"),
                )
            except HTTPException:
                raise
            except Exception as exc:
                logger.bind(ctx=ctx).error("Invalid MCP server {} error={}", mcp_server, exc)
                raise HTTPException(status_code=400, detail=f"Invalid MCP server definition: {mcp_server}")

        for tool in self.action_config.tools or []:
            try:
                if isinstance(tool, str) and tool.startswith(MCP_START_CONSTANT):
                    mcp_server_name = tool[len(MCP_START_CONSTANT):].strip()
                    user_mcp = UserMCP(user=self.user, db=self.db)
                    mcp_doc = await user_mcp.get_mcp_by_server_name(
                        ctx=ctx,
                        mcp_server_name=mcp_server_name,
                        decrypt=True,
                    )
                    if not mcp_doc:
                        raise HTTPException(
                            status_code=400,
                            detail=f"MCP server with name {mcp_server_name} not found",
                        )

                    _add_server(
                        name=mcp_doc.server_name,
                        url=mcp_doc.server_url,
                        authorization_token=mcp_doc.oauth_token,
                    )
                    _add_toolset(server_name=mcp_doc.server_name)
                else:
                    # Already-resolved `mcp_toolset` dicts (from a prior turn's
                    # enable_mcp + sync-back) and non-MCP tools are kept as-is.
                    sanitised_tools.append(tool)
            except HTTPException:
                raise
            except Exception as exc:
                logger.bind(ctx=ctx).error("Invalid MCP tool {} error={}", tool, exc)
                raise HTTPException(status_code=400, detail=f"Invalid tool definition: {tool}")

        if sanitised_mcp_servers:
            self.action_config.mcp_servers = sanitised_mcp_servers
            self.action_config.betas = list(
                dict.fromkeys((self.action_config.betas or []) + ["mcp-client-2025-11-20"])
            )
        else:
            # No MCP servers resolved for this run. Normalize any stale empty
            # list (e.g. persisted by an older sync-back that converted
            # `None -> []`) back to `None` so `model_dump(exclude_none=True)`
            # drops the field. Otherwise the API would reject an empty
            # `mcp_servers` array as "Extra inputs are not permitted" because
            # the model_validator only adds the MCP beta when mcp_servers is
            # truthy.
            self.action_config.mcp_servers = None

        self.action_config.tools = sanitised_tools

    @staticmethod
    def _has_code_execution_tool(tools: list[Any]) -> bool:
        return any(
            isinstance(t, dict)
            and t.get("type") == "code_execution_20250825"
            and t.get("name") == "code_execution"
            for t in tools
        )

    async def replace_tools_with_tools_defs(
        self,
        action_input: DataBlock,
        for_persistence: bool = False,
    ) -> List[Dict[str, Any]] | None:
        action_tools: list[Any] = []
        generate_def_for: list[str] = []
        tool_list: list[Any] = []

        default_tools: list[str] = list(CLAUDE_DEFAULT_TOOLS)

        def _tool_already_present(tool_name: str) -> bool:
            for tool in (self.action_config.tools or []) + (action_input.tools or []):
                if isinstance(tool, str) and tool == tool_name:
                    return True
                if isinstance(tool, dict) and tool.get("name") == tool_name:
                    return True
            return False

        for default_tool in default_tools:
            if not _tool_already_present(default_tool):
                tool_list.append(default_tool)

        capabilities = self._resolved_capabilities()
        if self.action_config.tools:
            tool_list.extend(self.action_config.tools)
        if action_input.tools:
            tool_list.extend(action_input.tools)


        has_auto_code_execution_via_web = any(
            self._web_tool_implies_auto_code_execution(t) for t in tool_list
        )

        for action_tool in tool_list:
            if isinstance(action_tool, str):
                if action_tool.startswith(MCP_START_CONSTANT):
                    action_tools.append(action_tool)
                    continue

                if action_tool == "code_interpreter":
                    if not capabilities.supports_code_execution:
                        logger.bind(
                            provider=self.action_config.provider,
                            model=self.action_config.model,
                        ).warning("Skipping unsupported Claude code execution tool")
                        continue

                    self._append_beta("code-execution-2025-08-25")

                    should_skip_for_runtime = (
                        has_auto_code_execution_via_web
                        and not for_persistence
                    )

                    if should_skip_for_runtime:
                        logger.bind(
                            provider=self.action_config.provider,
                            model=self.action_config.model,
                        ).info(
                            "Skipping explicit code_execution in runtime Claude request because latest web tools "
                            "already enable the internal code-execution path"
                        )
                        continue

                    if not self._has_code_execution_tool(action_tools):
                        action_tools.append(
                            {
                                "type": "code_execution_20250825",
                                "name": "code_execution",
                            }
                        )

                    continue

                if action_tool == "web_fetch_claude":
                    if not capabilities.supports_web_fetch:
                        logger.bind(
                            provider=self.action_config.provider,
                            model=self.action_config.model,
                        ).warning("Skipping unsupported Claude web fetch tool")
                        continue

                    tool_def = self._normalize_latest_web_tool(
                        {
                            "type": "web_fetch_20260209",
                            "name": "web_fetch",
                            "citations": {"enabled": bool(self.action_config.citations)},
                        },
                        is_fetch=True,
                    )
                    action_tools.append(tool_def)
                    continue

                if action_tool == "web_search_claude":
                    if not capabilities.supports_web_search:
                        logger.bind(
                            provider=self.action_config.provider,
                            model=self.action_config.model,
                        ).warning("Skipping unsupported Claude web search tool")
                        continue

                    tool_def = self._normalize_latest_web_tool(
                        {
                            "type": "web_search_20260209",
                            "name": "web_search",
                        },
                        is_fetch=False,
                    )
                    action_tools.append(tool_def)
                    continue

                if action_tool == "memory_tool_claude":
                    if not capabilities.supports_memory_tool:
                        logger.bind(
                            provider=self.action_config.provider,
                            model=self.action_config.model,
                        ).warning("Skipping unsupported Claude memory tool")
                        continue
                    self._append_beta("context-management-2025-06-27")
                    action_tools.append(
                        BetaMemoryTool20250818Param(type="memory_20250818", name="memory")
                    )
                    continue

                if action_tool in TOOLS_MAP:
                    generate_def_for.append(action_tool)
                    continue

                action_tools.append(action_tool)
                continue

            if isinstance(action_tool, dict):
                tool_copy = dict(action_tool)
                tool_type = tool_copy.get("type")

                if tool_type == "code_execution_20250522":
                    if not capabilities.supports_code_execution:
                        logger.bind(
                            provider=self.action_config.provider,
                            model=self.action_config.model,
                        ).warning("Skipping unsupported Claude code execution tool")
                        continue

                    tool_copy["type"] = "code_execution_20250825"
                    tool_copy.setdefault("name", "code_execution")
                    self._append_beta("code-execution-2025-08-25")

                    should_skip_for_runtime = (
                        has_auto_code_execution_via_web
                        and not for_persistence
                    )

                    if should_skip_for_runtime:
                        logger.bind(
                            provider=self.action_config.provider,
                            model=self.action_config.model,
                        ).info(
                            "Skipping explicit code_execution in runtime Claude request because latest web tools "
                            "already enable the internal code-execution path"
                        )
                        continue

                    if not self._has_code_execution_tool(action_tools):
                        action_tools.append(tool_copy)

                    continue

                if tool_type == "code_execution_20250825":
                    if not capabilities.supports_code_execution:
                        logger.bind(
                            provider=self.action_config.provider,
                            model=self.action_config.model,
                        ).warning("Skipping unsupported Claude code execution tool")
                        continue

                    tool_copy.setdefault("name", "code_execution")
                    self._append_beta("code-execution-2025-08-25")

                    should_skip_for_runtime = (
                        has_auto_code_execution_via_web
                        and not for_persistence
                    )

                    if should_skip_for_runtime:
                        logger.bind(
                            provider=self.action_config.provider,
                            model=self.action_config.model,
                        ).info(
                            "Skipping explicit code_execution in runtime Claude request because latest web tools "
                            "already enable the internal code-execution path"
                        )
                        continue

                    if not self._has_code_execution_tool(action_tools):
                        action_tools.append(tool_copy)

                    continue

                if tool_type == "web_search_20250305":
                    if not capabilities.supports_web_search:
                        logger.bind(
                            provider=self.action_config.provider,
                            model=self.action_config.model,
                        ).warning("Skipping unsupported Claude web search tool")
                        continue

                    # Upgrade to latest tool version, but normalize for compatibility.
                    tool_copy["type"] = "web_search_20260209"
                    tool_copy = self._normalize_latest_web_tool(
                        tool_copy,
                        is_fetch=False,
                    )
                    action_tools.append(tool_copy)
                    continue

                if tool_type == "web_fetch_20250910":
                    if not capabilities.supports_web_fetch:
                        logger.bind(
                            provider=self.action_config.provider,
                            model=self.action_config.model,
                        ).warning("Skipping unsupported Claude web fetch tool")
                        continue

                    tool_copy["type"] = "web_fetch_20260209"
                    tool_copy = self._normalize_latest_web_tool(
                        tool_copy,
                        is_fetch=True,
                    )
                    action_tools.append(tool_copy)
                    continue

                if tool_type == "web_search_20260209":
                    if not capabilities.supports_web_search:
                        logger.bind(
                            provider=self.action_config.provider,
                            model=self.action_config.model,
                        ).warning("Skipping unsupported Claude web search tool")
                        continue

                    tool_copy = self._normalize_latest_web_tool(
                        tool_copy,
                        is_fetch=False,
                    )
                    action_tools.append(tool_copy)
                    continue

                if tool_type == "web_fetch_20260209":
                    if not capabilities.supports_web_fetch:
                        logger.bind(
                            provider=self.action_config.provider,
                            model=self.action_config.model,
                        ).warning("Skipping unsupported Claude web fetch tool")
                        continue

                    tool_copy = self._normalize_latest_web_tool(
                        tool_copy,
                        is_fetch=True,
                    )
                    action_tools.append(tool_copy)
                    continue

                action_tools.append(tool_copy)
                continue

            action_tools.append(action_tool)

        tool_defs = await ToolFactoryClaude.get_tool_definations(generate_def_for)
        if tool_defs:
            action_tools.extend(tool_defs)

        return action_tools if action_tools else None

    async def run_tool(
        self,
        action_config: ClaudeChatConfig,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        raise NotImplementedError


    async def preprocess_input_messages(
        self,
        ctx: Context,
        action_input: DataBlock,
        claude_client: Claude | None = None,
    ) -> None:
        content: list[dict[str, Any]] = []

        container_runtime_context = self._build_container_runtime_context_block()
        if container_runtime_context is not None:
            content.append(container_runtime_context)

        vector_store_context = self._build_vector_store_context_block()
        if vector_store_context is not None:
            content.append(vector_store_context)

        generated_files_context = self._build_generated_files_context_block()
        if generated_files_context is not None:
            content.append(generated_files_context)

        # Anthropic requires every user message to have non-empty content.
        # Guard against empty/whitespace prompts (and prompts that are only URLs).
        urls = list(getattr(action_input, "urls", None) or [])
        raw_text = str(getattr(action_input, "text", "") or "")
        has_text = bool(raw_text.strip())

        if not has_text:
            if urls:
                prompt = f"User provided urls - [{', '.join(urls)}]"
            else:
                prompt = "Explain about your capabilities in brief and ask user to what user wants to do.."
        else:
            prompt = action_input.get_llm_prompt(ctx=ctx)

        prompt = await action_input.maybe_convert_input_to_file(
            ctx=ctx,
            user=self.user,
            prompt=prompt,
        )
        content.append({"type": "text", "text": prompt})

        self.action_config.messages += [ClaudeMessageParam(role="user", content=self._to_plain(content))]

    async def process_computer_use_config(self, ctx: Context, client: Claude) -> None:
        if self.action_config.computer_use is not None:
            claude_client = client.client
            if (
                not hasattr(claude_client.messages, "create")
                or "computer_use" not in inspect.signature(claude_client.messages.create).parameters
            ):
                logger.bind(ctx=ctx).warning(
                    "Computer use requested but current SDK does not expose computer_use"
                )
            else:
                logger.bind(ctx=ctx).info("Computer use enabled={}", self.action_config.computer_use)

    def get_default_headers(self) -> dict[str, str]:
        return {
            "anthropic-beta": "computer-use=true",
        }
