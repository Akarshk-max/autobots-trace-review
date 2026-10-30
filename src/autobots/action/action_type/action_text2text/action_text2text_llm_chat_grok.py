import json
import mimetypes
from typing import Any, Dict, List, Tuple, Type

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
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.grok.grok_chat.grok_chat import GrokChat
from src.autobots.conn.grok.grok_chat.grok_models import (
    DEFAULT_MODEL_REGISTRY,
    GrokMessage,
    GrokRole,
)
from src.autobots.conn.grok.grok_chat.grok_tool_spec_factory import (
    build_custom_tools_from_tools_map,
)
from src.autobots.conn.grok.grok_chat.grok_user_config import GrokUserConfig, compile_grok_plan
from src.autobots.conn.openai.openai_files.openai_files import OpenaiFiles
from src.autobots.conn.openai.openai_files.openai_files_model import FileCreate
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.common_default_tools import GROK_DEFAULT_TOOLS
from src.autobots.llm.tools.openai_vector_store_tool import (
    OpenAIVectorStoreFileInput,
    OpenAIVectorStoreToolConfig,
    OpenAIVectorStoreToolOperation,
    openai_vector_store_tool,
)
from src.autobots.llm.tools.tool_factory import ToolFactory


class GrokChatConfig(GrokUserConfig):
    pass


class ActionText2TextLlmChatGrok(
    ActionABC[GrokChatConfig, GrokChatConfig, GrokChatConfig, DataBlock, DataBlocks]
):
    type = ActionType.text2text_llm_chat_grok

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return GrokChatConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return GrokChatConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return GrokChatConfig

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
        curr_config: GrokChatConfig,
        prev_results: List[ActionResult] | None = None,
    ) -> GrokChatConfig:
        return curr_config

    def _to_plain(self, obj: Any) -> Any:
        if hasattr(obj, "model_dump"):
            try:
                obj = obj.model_dump(exclude_none=True)
            except Exception:
                pass
        if isinstance(obj, list):
            return [self._to_plain(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self._to_plain(v) for k, v in obj.items()}
        return obj

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
        """Capture the turn-1 prompt prefix (e.g. system message) exactly once."""
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

    def _minimize_persisted_messages(self) -> None:
        """messages is a working-set window rebuilt at runtime; turn_history +
        pinned_messages are canonical."""
        self.action_config.messages = []

    def _ensure_default_tool_selection(self) -> None:
        current = list(getattr(self.action_config, "tools", None) or [])
        for tool_name in GROK_DEFAULT_TOOLS:
            if tool_name not in current:
                current.append(tool_name)
        self.action_config.tools = current

    def _history_vector_store_enabled(self) -> bool:
        return bool(getattr(self.action_config, "history_vector_store_enabled", True))

    def _history_index_every_n_turns(self) -> int:
        raw = getattr(self.action_config, "history_vector_store_index_every_n_turns", 20)
        try:
            value = int(raw)
        except Exception:
            value = 20
        return max(1, min(200, value))

    def _extract_json_payload_from_datablocks(
        self, data_blocks: DataBlocks | Exception
    ) -> dict[str, Any]:
        if isinstance(data_blocks, Exception):
            raise data_blocks
        if not data_blocks.blocks or not data_blocks.blocks[0].text:
            raise HTTPException(status_code=500, detail="Expected JSON payload in DataBlocks")
        return json.loads(data_blocks.blocks[0].text)

    async def _ensure_history_vector_store_exists(self, ctx: Context, notify) -> None:
        if getattr(self.action_config, "history_vector_store_id", None):
            return

        name = (
            getattr(self.action_config, "history_vector_store_name", None)
            or f"grok-chat-history-{gen_uuid()}"
        )
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
        payload = self._extract_json_payload_from_datablocks(result)
        vector_store = payload.get("vector_store") or {}
        vector_store_id = payload.get("vector_store_id") or vector_store.get("id")

        if not vector_store_id:
            raise HTTPException(
                status_code=500, detail="Failed to create conversation history vector store"
            )

        self.action_config.history_vector_store_id = vector_store_id
        self.action_config.history_vector_store_name = vector_store.get("name") or name

    def _stringify_history_message_content(self, content: Any, *, tool_blocks_only: bool) -> str:
        text = str(content or "")
        return "" if tool_blocks_only else text

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
                    messages.append({"role": msg.get("role") or "user", "content": text.strip()})

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
        notify,
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

        await openai_vector_store_tool(
            config=OpenAIVectorStoreToolConfig(
                operation=OpenAIVectorStoreToolOperation.DELETE_VECTOR_STORE_FILES,
                vector_store_id=self.action_config.history_vector_store_id,
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
        ctx: Context,
        notify,
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
        notify,
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
            await self._ensure_history_vector_store_exists(ctx=ctx, notify=notify)

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

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify=None,
    ) -> DataBlocks | Exception:
        data_blocks = DataBlocks(blocks=[])
        turn_start_index = -1
        try:
            if not getattr(self.action_config, "chat_turn", None):
                self.action_config.chat_turn = 1

            self._ensure_pinned_messages_initialized()
            self._rebuild_working_messages_from_history()
            turn_start_index = len(self.action_config.messages or [])
            if app_state.cancellation is not None:
                app_state.cancellation.set_snapshot_meta(
                    turn_start_index=turn_start_index,
                    chat_turn=int(getattr(self.action_config, "chat_turn", 1) or 1),
                )
            current_turn = self._current_turn()

            self._ensure_default_tool_selection()

            grok_conn = await AppConn.get_grok(ctx=ctx, db=self.db, user=self.user)
            client = grok_conn.client

            user_cfg = GrokUserConfig(**self.action_config.model_dump(exclude_none=True))
            plan = compile_grok_plan(user_cfg, DEFAULT_MODEL_REGISTRY)
            req = plan.req

            prompt = ""
            if action_input:
                prompt = action_input.get_llm_prompt(ctx=ctx)

            req.messages = list(req.messages or [])
            if prompt:
                user_msg = GrokMessage(role=GrokRole.user, content=prompt)
                req.messages.append(user_msg)
                self.action_config.messages = list(self.action_config.messages or []) + [user_msg]

            urls = list(getattr(action_input, "urls", None) or [])
            if urls:
                img_urls, file_urls = self._split_image_vs_file_urls(urls)
                req.image_urls = list(req.image_urls or []) + img_urls
                req.file_urls = list(req.file_urls or []) + file_urls

            tool_runner = None
            if self.action_config.tools:
                tool_runner = ToolFactory(user=self.user, db=self.db)
                # here we go this is where everything abstracted out and it will handled in grok chat not in action
                req.custom_tools = build_custom_tools_from_tools_map(
                    tool_names=self.action_config.tools
                )

            if user_cfg.tools:
                tool_runner = ToolFactory(user=self.user, db=self.db)

            grok_chat = GrokChat(
                client=client,
                timeout_sec=req.timeout_sec or self.action_config.timeout_sec or 420.0,
            )

            chat_state: Dict[str, Any] = {
                "turn_history": self._to_plain(getattr(self.action_config, "turn_history", None) or []),
                "pinned_messages": self._to_plain(getattr(self.action_config, "pinned_messages", None) or []),
                "history_vector_store_id": getattr(self.action_config, "history_vector_store_id", None),
            }

            out = await grok_chat.run(
                ctx=ctx,
                app_state=app_state,
                user=self.user,
                req=req,
                db=self.db,
                notify=notify,
                tool_runner=tool_runner,
                max_rounds=user_cfg.max_rounds,
                plan=plan,
                chat_state=chat_state,
            )

            if not out or not out.content or not out.content.strip():
                raise HTTPException(
                    status_code=503, detail="No response from Grok, please try again."
                )

            # Persist assistant turn so next run is user -> assistant -> user
            self.action_config.messages = list(self.action_config.messages or []) + [
                GrokMessage(role=GrokRole.assistant, content=out.content)
            ]

            # Persist verbatim per-turn chat history (outside the model context by default).
            try:
                self._append_current_turn_to_history(turn=current_turn, start_index=turn_start_index)
                self._minimize_persisted_messages()
            except Exception as exc:
                logger.bind(ctx=ctx).warning("Failed persisting Grok turn history: {}", exc)

            await self._maybe_index_turn_history_to_vector_store(
                ctx=ctx, notify=notify, current_turn=current_turn
            )

            self.action_config.chat_turn = current_turn + 1

            data_blocks.blocks.append(DataBlock(text=out.content))
            return data_blocks

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in Grok run_action: {str(e)}")

            # Persist this run as a turn even on failure, so we don't lose the user's prompt.
            try:
                if turn_start_index >= 0:
                    current_turn = self._current_turn()
                    appended = self._append_current_turn_to_history(
                        turn=current_turn, start_index=turn_start_index
                    )
                    self._minimize_persisted_messages()
                    if appended:
                        self.action_config.chat_turn = current_turn + 1
            except Exception as history_exc:
                logger.bind(ctx=ctx).warning(
                    "Failed persisting partial Grok turn history after error: {}", history_exc
                )

            return e

    @staticmethod
    def _split_image_vs_file_urls(urls: List[str]) -> Tuple[List[str], List[str]]:
        img_urls: List[str] = []
        file_urls: List[str] = []

        for u in urls:
            mt, _ = mimetypes.guess_type(u)
            if mt and mt.startswith("image/"):
                img_urls.append(u)
            else:
                file_urls.append(u)

        return img_urls, file_urls
