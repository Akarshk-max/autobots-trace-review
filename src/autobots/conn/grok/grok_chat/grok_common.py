import asyncio
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Sequence, Tuple

import xai_sdk
from motor.motor_asyncio import AsyncIOMotorDatabase
from xai_sdk.chat import assistant, system
from xai_sdk.chat import file as xai_file
from xai_sdk.chat import image as xai_image
from xai_sdk.chat import tool as xai_tool
from xai_sdk.chat import user as chat_user
from xai_sdk.search import SearchParameters, news_source, rss_source, web_source, x_source

from src.autobots.conn.grok.grok_chat.grok_models import (
    DEFAULT_MODEL_REGISTRY,
    CustomToolSpec,
    GrokAttachment,
    GrokChatRequest,
    GrokChatResult,
    GrokMessage,
    GrokRole,
    GrokUsage,
    ModelRegistry,
)
from src.autobots.conn.grok.grok_files.grok_file import XAIFileManager
from src.autobots.conn.grok.live_search.live_search_models import (
    LiveSearchParametersConfig,
    NewsSourceConfig,
    RSSSourceConfig,
    WebSourceConfig,
    XAILiveSearchRequestError,
    XSourceConfig,
)
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


class GrokChatError(RuntimeError):
    pass


def _safe_notify(notify: Optional[Notify], ctx: Context, msg: str) -> None:
    if not notify:
        return
    try:
        notify(ctx, msg)
    except Exception:
        pass


def _evt(notify: Optional[Notify], ctx: Context, name: str, data: Dict[str, Any]) -> None:
    """
    Tiny event messages over the existing Notify (which truncates to 500 chars).
    UI can parse these if it wants: messages prefixed with '@@evt '.
    """
    if not notify:
        return
    import json

    payload = {"t": name, "d": data}
    s = "@@evt " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        notify(ctx, s[:480])
    except Exception:
        pass


def _coerce_usage(resp: Any) -> Optional[GrokUsage]:
    try:
        u = getattr(resp, "usage", None)
        if not u:
            return None
        return GrokUsage(
            prompt_tokens=int(getattr(u, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(u, "completion_tokens", 0) or 0),
            total_tokens=int(getattr(u, "total_tokens", 0) or 0),
            reasoning_tokens=int(getattr(u, "reasoning_tokens", 0) or 0),
            num_sources_used=getattr(u, "num_sources_used", None),
        )
    except Exception:
        return None


def _coerce_tool_calls(resp: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        tcs = getattr(resp, "tool_calls", None) or []
        for tc in tcs:
            fn = getattr(tc, "function", None)
            out.append(
                {
                    "id": getattr(tc, "id", None),
                    "function": {
                        "name": getattr(fn, "name", None) if fn else None,
                        "arguments": getattr(fn, "arguments", None) if fn else None,
                    },
                }
            )
    except Exception:
        pass
    return out


def _is_image_path_or_url(s: str) -> bool:
    s = (s or "").lower()
    # all the extensions of image files i can remember for now added here
    return any(
        s.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"]
    )


async def _iterate_stream(chat: Any) -> AsyncGenerator[Tuple[Any, Any], None]:
    """
    Robust streaming iterator:
    - If stream() returns async iterator -> async for
    - If sync iterator -> bridge via thread + queue WITHOUT blocking until completion
    """
    stream_obj = chat.stream()

    if hasattr(stream_obj, "__aiter__"):
        async for item in stream_obj:
            yield item
        return

    if hasattr(stream_obj, "__iter__"):
        q: asyncio.Queue = asyncio.Queue()
        SENTINEL = object()

        def _run_sync():
            try:
                for item in stream_obj:
                    q.put_nowait(item)
            finally:
                q.put_nowait(SENTINEL)

        bg = asyncio.create_task(asyncio.to_thread(_run_sync))

        try:
            while True:
                item = await q.get()
                if item is SENTINEL:
                    break
                yield item
        finally:
            if not bg.done():
                bg.cancel()
        return

    raise GrokChatError("chat.stream() is not iterable")


class GrokCommon:
    def __init__(
        self,
        *,
        model_registry: ModelRegistry = DEFAULT_MODEL_REGISTRY,
        file_manager: Optional[XAIFileManager] = None,
    ) -> None:
        self.model_registry = model_registry
        self.file_manager = file_manager or XAIFileManager()

    async def _ensure_last_user_message(self, req: GrokChatRequest) -> None:
        if any(m.role == GrokRole.user for m in req.messages):
            return
        req.messages.append(GrokMessage(role=GrokRole.user, content=""))

    async def _inject_convenience_attachments(self, req: GrokChatRequest) -> None:
        """
        Attach req.file_* and req.image_* to the *last user message*.
        """
        if not (
            req.file_urls
            or req.file_ids
            or req.local_paths
            or req.image_urls
            or req.image_file_ids
            or req.image_local_paths
        ):
            return

        await self._ensure_last_user_message(req)

        idx = max(i for i, m in enumerate(req.messages) if m.role == GrokRole.user)
        msg = req.messages[idx]

        # files
        if req.use_file_grok:
            for fid in req.file_ids:
                msg.attachments.append(GrokAttachment(kind="file_id", value=fid))
            for url in req.file_urls:
                msg.attachments.append(GrokAttachment(kind="url", value=url))
            for path in req.local_paths:
                msg.attachments.append(GrokAttachment(kind="path", value=path))

        # images (mark is_image=True)
        for fid in req.image_file_ids:
            msg.attachments.append(GrokAttachment(kind="file_id", value=fid, is_image=True))
        for url in req.image_urls:
            msg.attachments.append(GrokAttachment(kind="url", value=url, is_image=True))
        for path in req.image_local_paths:
            msg.attachments.append(GrokAttachment(kind="path", value=path, is_image=True))

        req.messages[idx] = msg

    def build_server_tool_objects(self, cfg: Any) -> List[Any]:
        out: List[Any] = []
        if not cfg:
            return out

        try:
            from xai_sdk.tools import code_execution, web_search, x_search
        except Exception:
            return out

        if getattr(cfg, "web_search", False):
            out.append(web_search())
        if getattr(cfg, "x_search", False):
            out.append(x_search())
        if getattr(cfg, "code_execution", False):
            out.append(code_execution())
        return out

    def _build_search_parameters(self, cfg: LiveSearchParametersConfig) -> SearchParameters:
        """
        Same logic as your XAILiveSearchManager._build_search_parameters(),
        but embedded here so GrokChat can enable live_search with SearchParameters.

        It is just copy paste from my live search code base to integrate here with minimal tweaks.
        """
        try:
            sources = None
            if cfg.sources is not None:
                built = []
                for s in cfg.sources:
                    if isinstance(s, WebSourceConfig):
                        kwargs = {}
                        if s.country is not None:
                            kwargs["country"] = s.country
                        if s.excluded_websites:
                            kwargs["excluded_websites"] = s.excluded_websites
                        built.append(web_source(**kwargs))
                    elif isinstance(s, NewsSourceConfig):
                        kwargs = {}
                        if s.country is not None:
                            kwargs["country"] = s.country
                        if s.excluded_websites:
                            kwargs["excluded_websites"] = s.excluded_websites
                        built.append(news_source(**kwargs))
                    elif isinstance(s, XSourceConfig):
                        kwargs = {}
                        if s.included_x_handles:
                            kwargs["included_x_handles"] = s.included_x_handles
                        if s.excluded_x_handles:
                            kwargs["excluded_x_handles"] = s.excluded_x_handles
                        if s.post_favorite_count is not None:
                            kwargs["post_favorite_count"] = s.post_favorite_count
                        built.append(x_source(**kwargs))
                    elif isinstance(s, RSSSourceConfig):
                        if not s.links:
                            raise XAILiveSearchRequestError("rss.links cannot be empty")
                        built.append(rss_source(links=s.links))
                    else:
                        raise XAILiveSearchRequestError(f"Unknown source type: {type(s)}")

                sources = built

            kwargs: Dict[str, Any] = {
                "mode": cfg.mode,
                "return_citations": cfg.return_citations,
            }
            if cfg.from_date is not None:
                kwargs["from_date"] = cfg.from_date
            if cfg.to_date is not None:
                kwargs["to_date"] = cfg.to_date
            if sources is not None:
                kwargs["sources"] = sources

            return SearchParameters(**kwargs)
        except Exception as exc:
            raise GrokChatError(f"Invalid live_search config: {exc}") from exc

    async def _upload_attachment(
        self,
        *,
        ctx: Context,
        user_: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        att: GrokAttachment,
        notify: Optional[Notify],
    ) -> str:
        """
        Uploads attachment (if needed) and returns xAI file_id.
        """
        if att.kind == "file_id":
            return att.value

        _safe_notify(notify, ctx, "Uploading attachment to xAI Files...")

        if att.kind == "path":
            meta = await self.file_manager.upload(
                ctx=ctx,
                user=user_,
                db=db,
                client=client,
                source=att.value,
                filename=att.filename,
            )
            return meta.id

        if att.kind == "url":
            res = await self.file_manager.upload_urls(
                ctx=ctx,
                user=user_,
                db=db,
                client=client,
                urls=[att.value],
            )
            if att.value not in res:
                raise GrokChatError(f"Failed to upload url={att.value}")
            return res[att.value].id

        raise GrokChatError(f"Unknown attachment kind={att.kind}")

    def build_custom_tool_objects(self, specs: Sequence[CustomToolSpec]) -> List[Any]:
        out: List[Any] = []
        for spec in specs:
            out.append(
                xai_tool(
                    name=spec.name,
                    description=spec.description or None,
                    parameters=spec.parameters_schema or {"type": "object", "properties": {}},
                )
            )
        return out

    async def build_chat(
        self,
        *,
        client: xai_sdk.AsyncClient,
        req: GrokChatRequest,
        tools: Optional[List[Any]] = None,
        search_parameters: Optional[SearchParameters] = None,
    ) -> Any:
        resolved_model = self.model_registry.resolve(req.model)

        kwargs: Dict[str, Any] = {"model": resolved_model}
        if tools:
            kwargs["tools"] = tools
        if req.include:
            kwargs["include"] = list(req.include)
        if search_parameters is not None:
            kwargs["search_parameters"] = search_parameters

        return client.chat.create(**kwargs)

    async def append_messages(
        self,
        *,
        ctx: Context,
        user_: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        chat: Any,
        messages: Sequence[GrokMessage],
        notify: Optional[Notify],
        model_name: str,
    ) -> None:
        """
        Appends messages to chat
        uploads attachments and attaches via xai_sdk.chat.file(file_id).
        For images: attach the image file_id and use a vision-capable model.
        This is awesome part here we control how model will selected and how it will generate the chat
        and also recovering from few issues which can be caught in future.
        """
        supports_vision = self.model_registry.supports_vision(model_name)

        for m in messages:
            parts: List[Any] = []
            text = (m.content or "").strip()

            if m.attachments:
                for att in m.attachments:
                    # determine if image
                    is_img = att.is_image
                    if is_img is None and att.kind in ("url", "path"):
                        is_img = _is_image_path_or_url(att.value)

                    if is_img and not supports_vision:
                        raise GrokChatError(
                            f"Model '{model_name}' is not vision-capable, but an image attachment was provided."
                        )

                    if att.kind == "url" and is_img:
                        parts.append(xai_image(att.value, detail=att.detail))
                        continue

                    fid = await self._upload_attachment(
                        ctx=ctx,
                        user_=user_,
                        db=db,
                        client=client,
                        att=att,
                        notify=notify,
                    )
                    parts.append(xai_file(fid))

            if m.role == GrokRole.system:
                chat.append(system(text))
            elif m.role == GrokRole.user:
                if parts:
                    chat.append(chat_user(text, *parts))
                else:
                    chat.append(chat_user(text))
            elif m.role == GrokRole.assistant:
                chat.append(assistant(text))
            else:
                chat.append(chat_user(text))

    async def sample(self, *, chat: Any, req: GrokChatRequest) -> Any:
        """
        now deprecated
         kwargs: Dict[str, Any] = {}
         if req.temperature is not None:
             kwargs["temperature"] = req.temperature
         if req.max_tokens is not None:
             kwargs["max_tokens"] = req.max_tokens
         if req.top_p is not None:
             kwargs["top_p"] = req.top_p
         if req.frequency_penalty is not None:
             kwargs["frequency_penalty"] = req.frequency_penalty
         if req.presence_penalty is not None:
             kwargs["presence_penalty"] = req.presence_penalty
         if req.stop is not None:
             kwargs["stop"] = req.stop
         if req.response_format is not None:
             kwargs["response_format"] = req.response_format
        """

        return await chat.sample()

    async def stream_text(
        self, *, ctx: Context, chat: Any, notify: Optional[Notify]
    ) -> Tuple[str, Any]:
        """
        Batched streaming with full-content updates + TTFT event.
        """

        chunks: List[str] = []
        buffer: List[str] = []
        last_response: Any = None

        t0 = time.perf_counter()
        last_flush = t0
        sent_first = False

        FLUSH_CHARS = 180
        FLUSH_INTERVAL = 0.04

        async for response, chunk in _iterate_stream(chat):
            last_response = response

            txt = getattr(chunk, "content", None)
            if not txt:
                continue

            if not sent_first:
                sent_first = True
                ttft_ms = int((time.perf_counter() - t0) * 1000)
                _evt(notify, ctx, "ttft", {"ms": ttft_ms})

            buffer.append(txt)
            chunks.append(txt)

            now = time.perf_counter()
            buffered_text = "".join(buffer)

            should_flush = (
                len(buffered_text) >= FLUSH_CHARS
                or "\n" in buffered_text
                or (now - last_flush) >= FLUSH_INTERVAL
            )

            if should_flush:
                _safe_notify(notify, ctx, "".join(chunks))
                buffer.clear()
                last_flush = now

        if buffer:
            _safe_notify(notify, ctx, "".join(chunks))

        if last_response is None:
            raise GrokChatError("Streaming produced no final response")

        final_text = getattr(last_response, "content", None) or "".join(chunks)
        return final_text, last_response

    def to_result(self, resp: Any, content: str) -> GrokChatResult:
        citations = []
        try:
            citations = list(getattr(resp, "citations", None) or [])
        except Exception:
            citations = []

        return GrokChatResult(
            content=(content or "").strip(),
            model=str(getattr(resp, "model", "") or ""),
            citations=citations,
            tool_calls=_coerce_tool_calls(resp),
            usage=_coerce_usage(resp),
            raw={},
        )
