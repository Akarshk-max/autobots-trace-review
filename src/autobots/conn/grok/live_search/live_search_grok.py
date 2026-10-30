from typing import Optional

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from xai_sdk import AsyncClient
from xai_sdk.chat import user as chat_user
from xai_sdk.search import SearchParameters, news_source, rss_source, web_source, x_source

from src.autobots.conn.grok.live_search.live_search_models import (
    LiveSearchParametersConfig,
    LiveSearchRequest,
    LiveSearchResponse,
    LiveSearchStreamResponse,
    NewsSourceConfig,
    RSSSourceConfig,
    WebSourceConfig,
    XAILiveSearchError,
    XAILiveSearchRequestError,
    XAILiveSearchRunError,
    XSourceConfig,
)
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


class XAILiveSearchManager:
    """
    Wrapper class for handling how we can handle
    live search feature of xai cleint like live search , news search , with streaming options also,
    web searching also , x searching also , rss searching also.
    """

    async def run(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: AsyncClient,
        *,
        req: LiveSearchRequest,
        include: Optional[list[str]] = None,
    ) -> LiveSearchResponse:
        try:
            sp = self._build_search_parameters(ctx, user, db, req.search_parameters)

            chat = client.chat.create(
                model=req.model,
                search_parameters=sp,
                include=include,
            )
            chat.append(chat_user(req.prompt))

            response = await chat.sample()

            return LiveSearchResponse(
                request=req,
                content=getattr(response, "content", "") or "",
                citations=list(getattr(response, "citations", []) or []),
                num_sources_used=getattr(
                    getattr(response, "usage", None), "num_sources_used", None
                ),
            )
        except XAILiveSearchError:
            raise
        except Exception as exc:
            logger.exception(f"[LIVE-SEARCH] run failed error={exc}")
            raise XAILiveSearchRunError("Live search failed") from exc

    async def stream_to_response(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: AsyncClient,
        *,
        req: LiveSearchRequest,
        include: Optional[list[str]] = None,
    ) -> LiveSearchStreamResponse:
        try:
            sp = self._build_search_parameters(ctx, user, db, req.search_parameters)

            chat = client.chat.create(
                model=req.model,
                search_parameters=sp,
                include=include,
            )
            chat.append(chat_user(req.prompt))

            chunks: list[str] = []
            latest_response = None

            async for response, chunk in chat.stream():
                latest_response = response
                if getattr(chunk, "content", None):
                    chunks.append(chunk.content)

            if latest_response is None:
                raise XAILiveSearchRunError("Streaming produced no final response")

            content = getattr(latest_response, "content", None)
            if not content:
                content = "".join(chunks)

            return LiveSearchStreamResponse(
                request=req,
                content=content,
                chunks=chunks,
                citations=list(getattr(latest_response, "citations", []) or []),
                num_sources_used=getattr(
                    getattr(latest_response, "usage", None), "num_sources_used", None
                ),
            )
        except XAILiveSearchError:
            raise
        except Exception as exc:
            logger.exception(f"[LIVE-SEARCH] stream failed error={exc}")
            raise XAILiveSearchRunError("Live search streaming failed") from exc

    async def stream_chunks(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: AsyncClient,
        *,
        req: LiveSearchRequest,
        include: Optional[list[str]] = None,
    ):
        sp = self._build_search_parameters(ctx, user, db, req.search_parameters)

        chat = client.chat.create(
            model=req.model,
            search_parameters=sp,
            include=include,
        )
        chat.append(chat_user(req.prompt))

        async for _response, chunk in chat.stream():
            if getattr(chunk, "content", None):
                yield chunk.content

    def _build_search_parameters(
        self, ctx: Context, user: UserORM, db: AsyncIOMotorDatabase, cfg: LiveSearchParametersConfig
    ) -> SearchParameters:
        try:
            sources = None
            if cfg.sources is not None:
                sources = [self._build_source(s) for s in cfg.sources]

            kwargs = {
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
            raise XAILiveSearchRequestError(f"Invalid live search config: {exc}") from exc

    def _build_source(self, s):
        if isinstance(s, WebSourceConfig):
            kwargs = {}
            if s.country is not None:
                kwargs["country"] = s.country
            if s.excluded_websites:
                kwargs["excluded_websites"] = s.excluded_websites
            return web_source(**kwargs)

        if isinstance(s, NewsSourceConfig):
            kwargs = {}
            if s.country is not None:
                kwargs["country"] = s.country
            if s.excluded_websites:
                kwargs["excluded_websites"] = s.excluded_websites
            return news_source(**kwargs)

        if isinstance(s, XSourceConfig):
            kwargs = {}
            if s.included_x_handles:
                kwargs["included_x_handles"] = s.included_x_handles
            if s.excluded_x_handles:
                kwargs["excluded_x_handles"] = s.excluded_x_handles
            if s.post_favorite_count is not None:
                kwargs["post_favorite_count"] = s.post_favorite_count
            return x_source(**kwargs)

        if isinstance(s, RSSSourceConfig):
            if not s.links:
                raise XAILiveSearchRequestError("rss.links cannot be empty")
            return rss_source(links=s.links)

        raise XAILiveSearchRequestError(f"Unknown source type: {type(s)}")
