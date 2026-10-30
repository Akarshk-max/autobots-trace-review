from typing import Any, List, Optional, Tuple

import xai_sdk
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.grok.grok_chat.grok_chat import GrokChat, GrokChatError
from src.autobots.conn.grok.grok_chat.grok_collection_chat_config import CompiledCollectionPlan
from src.autobots.conn.grok.grok_chat.grok_common import _safe_notify
from src.autobots.conn.grok.grok_chat.grok_models import (
    GrokChatRequest,
    GrokChatResult,
    GrokMessage,
    GrokRole,
)
from src.autobots.conn.grok.grok_collection.collection_models import SearchResponse
from src.autobots.conn.grok.grok_collection.grok_collection import XAICollectionsManager
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

class GrokCollectionChat:
    def __init__(
        self,
        *,
        client: xai_sdk.AsyncClient,
        collections: Optional[XAICollectionsManager] = None,
        timeout_sec: float = 420.0,
        model_registry=None,
    ) -> None:
        self.client = client
        self.collections = collections or XAICollectionsManager()
        self.base_chat = GrokChat(
            client=client, timeout_sec=timeout_sec, model_registry=model_registry
        )
        self.timeout_sec = timeout_sec

    async def run(
        self,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        *,
        db: AsyncIOMotorDatabase,
        req: GrokChatRequest,
        plan: CompiledCollectionPlan,
        notify: Optional[Notify] = None,
        tool_runner: Optional[Any] = None,
        max_rounds: int = 10,
    ) -> GrokChatResult:
        if db is None:
            raise GrokChatError("db is required (AsyncIOMotorDatabase)")

        c = plan.collection

        # If collection augmentation disabled, just running normal chat fallback behaviour
        if not c.enabled:
            return await self.base_chat.run(
                ctx=ctx,
                app_state=app_state,
                user=user,
                req=req,
                db=db,
                notify=notify,
                tool_runner=tool_runner,
                max_rounds=max_rounds,
            )

        target_collection_id = c.collection_ids[0] if c.collection_ids else None
        if not target_collection_id:
            raise GrokChatError("Collection augmentation enabled but no collection_ids available")

        if c.add_existing_file_ids:
            _safe_notify(
                notify, ctx, f"Adding {len(c.add_existing_file_ids)} existing docs to collection..."
            )
            for fid in c.add_existing_file_ids:
                await self.collections.add_existing_document(
                    ctx=ctx,
                    user=user,
                    db=db,
                    client=self.client,
                    collection_id=target_collection_id,
                    file_id=fid,
                )
                logger.bind(ctx=ctx, user=user).info(
                    "[GROK CHAT] Files are added in existing documents list."
                )

        if c.upload_urls:
            _safe_notify(notify, ctx, f"Uploading {len(c.upload_urls)} URLs to collection...")
            await self.collections.upload_urls_to_collection(
                ctx=ctx,
                user=user,
                db=db,
                client=self.client,
                collection_id=target_collection_id,
                urls=c.upload_urls,
                wait_for_indexing=c.wait_for_indexing,
            )

        query = self._latest_user_text(req.messages)
        if not query.strip():
            return await self.base_chat.run(
                ctx=ctx,
                app_state=app_state,
                user=user,
                req=req,
                db=db,
                notify=notify,
                tool_runner=tool_runner,
                max_rounds=max_rounds,
            )

        _safe_notify(notify, ctx, "Searching collections...")
        search_resp = await self.collections.search(
            ctx=ctx,
            user=user,
            db=db,
            client=self.client,
            query=query,
            collection_ids=c.collection_ids,
            limit=c.search_limit,
            instructions=c.instructions,
            retrieval_mode=c.retrieval_mode,
        )

        injection_msg, retrieved_file_ids = self._build_injection_message(search_resp, c)
        if injection_msg is not None:
            req.messages = self._inject_before_last_user(req.messages, injection_msg)

        out = await self.base_chat.run(
            ctx=ctx,
            app_state=app_state,
            user=user,
            req=req,
            db=db,
            notify=notify,
            tool_runner=tool_runner,
            max_rounds=max_rounds,
            plan=plan.chat_plan,
        )

        if c.attach_retrieval_citations and retrieved_file_ids:
            out.citations = list(dict.fromkeys(list(out.citations or []) + retrieved_file_ids))

        return out

    @staticmethod
    def _latest_user_text(messages: List[GrokMessage]) -> str:
        for m in reversed(messages or []):
            if getattr(m, "role", None) == GrokRole.user and (m.content or "").strip():
                return m.content
        return ""

    @staticmethod
    def _inject_before_last_user(
        messages: List[GrokMessage], injection: GrokMessage
    ) -> List[GrokMessage]:
        msgs = list(messages or [])
        last_user_idx = None
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].role == GrokRole.user:
                last_user_idx = i
                break

        if last_user_idx is None:
            return msgs + [injection]

        return msgs[:last_user_idx] + [injection] + msgs[last_user_idx:]

    @staticmethod
    def _truncate(s: str, limit: int) -> str:
        s = s or ""
        if len(s) <= limit:
            return s
        return s[: max(0, limit - 3)] + "..."

    def _build_injection_message(
        self,
        resp: SearchResponse,
        cfg,
    ) -> Tuple[Optional[GrokMessage], List[str]]:
        matches = list(getattr(resp, "matches", []) or [])
        if not matches:
            return None, []

        retrieved_file_ids: List[str] = []

        lines: List[str] = []
        lines.append(f"{cfg.inject_title}:")
        lines.append(
            "Use ONLY this retrieved context when relevant. "
            "If you use a fact from a chunk, cite it by file_id in brackets like [file_id]."
        )
        lines.append("")

        total_chars = 0
        used = 0

        for m in matches:
            if used >= cfg.max_chunks:
                break

            fid = getattr(m, "file_id", "") or ""
            score = getattr(m, "score", None)
            chunk = getattr(m, "chunk_content", "") or ""

            if fid:
                retrieved_file_ids.append(fid)

            chunk = self._truncate(chunk.strip(), cfg.max_chars_per_chunk)

            header_parts = []
            if cfg.include_file_ids and fid:
                header_parts.append(f"file_id={fid}")
            if cfg.include_scores and score is not None:
                header_parts.append(f"score={float(score):.4f}")

            header = ("; ".join(header_parts)).strip()
            if header:
                lines.append(f"- {header}")
            else:
                lines.append("- match")

            lines.append(f"  {chunk}")
            lines.append("")

            total_chars += len(chunk)
            used += 1
            if total_chars >= cfg.max_total_chars:
                break

        content = "\n".join(lines).strip()
        role = cfg.inject_role
        return GrokMessage(role=role, content=content), list(dict.fromkeys(retrieved_file_ids))
