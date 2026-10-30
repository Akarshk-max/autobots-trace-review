"""Turn-history vector-store indexer for OpenRouter.

Indexes older conversation turns into a dedicated OpenAI vector store so the
model can semantically search turns that have scrolled out of the visible
context window. All indexing is best-effort: failures must never break a turn.
"""
import json
from typing import Any

from loguru import logger

from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.openai.openai_files.openai_files import OpenaiFiles
from src.autobots.conn.openai.openai_files.openai_files_model import FileCreate
from src.autobots.conn.openrouter.chat_model import OpenRouterChatReq
from src.autobots.conn.openrouter.utils import to_plain
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.openai_vector_store_tool import (
    OpenAIVectorStoreFileInput,
    OpenAIVectorStoreToolConfig,
    OpenAIVectorStoreToolOperation,
    openai_vector_store_tool,
)
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


class HistoryVectorStoreIndexer:
    """Best-effort turn-history indexing into a dedicated OpenAI vector store."""

    @staticmethod
    def _stringify_message_content(content: Any, *, tool_blocks_only: bool) -> str:
        if isinstance(content, str):
            return "" if tool_blocks_only else content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                plain = to_plain(block)
                block_type = plain.get("type") if isinstance(plain, dict) else None
                is_tool_block = block_type in {"tool_use", "tool_result"}
                if is_tool_block != tool_blocks_only:
                    continue
                part = HistoryVectorStoreIndexer._stringify_content_block(block)
                if part:
                    parts.append(part)
            return "\n".join(parts)
        return ""

    @staticmethod
    def _stringify_content_block(block: Any) -> str:
        block = to_plain(block)
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
                parts = [HistoryVectorStoreIndexer._stringify_content_block(c) for c in content]
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

    @classmethod
    def _collect_turns(
        cls, *, turns: list[dict[str, Any]], tool_blocks_only: bool
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        for raw_turn in turns:
            turn = to_plain(raw_turn)
            if not isinstance(turn, dict):
                continue
            messages: list[dict[str, str]] = []
            for raw_msg in turn.get("messages") or []:
                msg = to_plain(raw_msg)
                if not isinstance(msg, dict):
                    continue
                text = cls._stringify_message_content(
                    msg.get("content"), tool_blocks_only=tool_blocks_only
                )
                if text.strip():
                    messages.append({"role": msg.get("role") or "unknown", "content": text.strip()})
            if messages:
                collected.append({"turn": turn.get("turn"), "messages": messages})
        return collected

    @classmethod
    def _render_messages_json(cls, *, turns: list[dict[str, Any]]) -> str:
        collected = cls._collect_turns(turns=turns, tool_blocks_only=False)
        return json.dumps(collected, ensure_ascii=False, default=str)

    @classmethod
    def _render_tool_activity_text(cls, *, turns: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for turn in cls._collect_turns(turns=turns, tool_blocks_only=True):
            lines.append(f"=== Turn {turn['turn']} ===")
            lines.extend(f"[{msg['role']}] {msg['content']}" for msg in turn["messages"])
            lines.append("")
        return "\n".join(lines).strip()

    @staticmethod
    async def _upload_file(
        ctx: Context,
        user: UserORM,
        db: Any | None,
        *,
        filename: str,
        content: str,
        turn_start: int,
        turn_end: int,
        content_type: str,
    ) -> OpenAIVectorStoreFileInput:
        openai_conn = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider="openai")
        if isinstance(openai_conn, Exception):
            raise openai_conn
        openai_files = OpenaiFiles(openai_client=openai_conn.client)
        file_obj = await openai_files.create(
            ctx=ctx,
            file_create=FileCreate(
                file=(filename, content.encode("utf-8")), purpose="assistants"
            ),
        )
        return OpenAIVectorStoreFileInput(
            openai_file_id=file_obj.id,
            attributes={"turn_start": turn_start, "turn_end": turn_end, "content_type": content_type},
        )

    @staticmethod
    async def _replace_stale_files(
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: Any | None,
        notify: Notify | None,
        *,
        history_vector_store_id: str,
        turn_start: int,
        turn_end: int,
        content_types: set[str],
    ) -> None:
        list_result = await openai_vector_store_tool(
            config=OpenAIVectorStoreToolConfig(
                operation=OpenAIVectorStoreToolOperation.LIST_VECTOR_STORE_FILES,
                vector_store_id=history_vector_store_id,
            ),
            ctx=ctx,
            app_state=app_state,
            user=user,
            db=db,
            notify=notify,
        )
        if isinstance(list_result, Exception) or not list_result.blocks or not list_result.blocks[0].text:
            return
        payload = json.loads(list_result.blocks[0].text)

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
                vector_store_id=history_vector_store_id,
                delete_file_ids=stale_ids,
            ),
            ctx=ctx,
            app_state=app_state,
            user=user,
            db=db,
            notify=notify,
        )
        logger.bind(
            ctx=ctx, turn_start=turn_start, turn_end=turn_end, stale_file_count=len(stale_ids)
        ).info("Removed stale history vector-store files before re-indexing")

    @classmethod
    async def index_batch(
        cls,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: Any | None,
        notify: Notify | None,
        *,
        history_vector_store_id: str,
        turns: list[dict[str, Any]],
        turn_start: int,
        turn_end: int,
    ) -> None:
        candidates = (
            (cls._render_messages_json(turns=turns), "messages", "json"),
            (cls._render_tool_activity_text(turns=turns), "tool_activity", "md"),
        )
        non_empty = [
            (content, content_type, ext)
            for content, content_type, ext in candidates
            if content.strip() and content.strip() != "[]"
        ]
        if not non_empty:
            return

        await cls._replace_stale_files(
            ctx,
            app_state,
            user,
            db,
            notify,
            history_vector_store_id=history_vector_store_id,
            turn_start=turn_start,
            turn_end=turn_end,
            content_types={content_type for _, content_type, _ in non_empty},
        )

        files_to_attach = [
            await cls._upload_file(
                ctx,
                user,
                db,
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
                vector_store_id=history_vector_store_id,
                files=files_to_attach,
                wait_for_completion=True,
            ),
            ctx=ctx,
            app_state=app_state,
            user=user,
            db=db,
            notify=notify,
        )
        if isinstance(result, Exception):
            logger.bind(ctx=ctx, turn_start=turn_start, turn_end=turn_end).warning(
                "History vector-store indexing failed: {}", result
            )
            return
        payload = json.loads(result.blocks[0].text) if result.blocks and result.blocks[0].text else {}
        if not payload.get("ok", False):
            logger.bind(ctx=ctx, turn_start=turn_start, turn_end=turn_end).warning(
                "History vector-store indexing reported failure: {}", payload
            )

    @staticmethod
    async def ensure_store_exists(
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: Any | None,
        notify: Notify | None,
        *,
        chat_req: OpenRouterChatReq,
    ) -> None:
        if getattr(chat_req, "history_vector_store_id", None):
            return

        name = getattr(chat_req, "history_vector_store_name", None) or f"openrouter-chat-history-{gen_uuid()}"
        result = await openai_vector_store_tool(
            config=OpenAIVectorStoreToolConfig(
                operation=OpenAIVectorStoreToolOperation.CREATE_VECTOR_STORE,
                name=name,
            ),
            ctx=ctx,
            app_state=app_state,
            user=user,
            db=db,
            notify=notify,
        )
        if isinstance(result, Exception):
            raise result
        if not result.blocks or not result.blocks[0].text:
            raise RuntimeError("Failed to create conversation history vector store")
        payload = json.loads(result.blocks[0].text)
        vector_store = payload.get("vector_store") if isinstance(payload.get("vector_store"), dict) else payload
        vector_store_id = str(vector_store.get("id") or "").strip()
        if not vector_store_id:
            raise RuntimeError("Failed to create conversation history vector store")

        chat_req.history_vector_store_id = vector_store_id
        chat_req.history_vector_store_name = vector_store.get("name") or name
