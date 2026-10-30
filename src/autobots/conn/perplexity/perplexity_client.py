import json
from typing import Any, AsyncIterator

import httpx
from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


class PerplexitySDKClient:
    def __init__(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        api_key: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        base_url: str = "https://api.perplexity.ai",
    ) -> None:
        self.ctx = ctx
        self.user = user
        self.db = db
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries or 0

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout or 3600.0,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _extract_error_detail(self, response: httpx.Response) -> Any:
        try:
            await response.aread()
        except Exception:
            pass

        try:
            payload = response.json()
            if isinstance(payload, dict):
                return payload.get("error") or payload.get("detail") or payload
            return payload
        except Exception:
            pass

        try:
            text = response.text
            if text:
                return text
        except Exception:
            pass

        return f"Perplexity API error {response.status_code}"


    async def _raise_api_error(self, *, ctx: Context, response: httpx.Response) -> None:
        if response.is_success:
            return

        detail = await self._extract_error_detail(response)

        logger.bind(
            ctx=ctx,
            status_code=response.status_code,
            detail=detail,
        ).error("Perplexity API request failed")

        raise HTTPException(status_code=response.status_code, detail=detail)

    async def _post_json(self, *, ctx: Context, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        logger.bind(ctx=ctx, path=path).info("Calling Perplexity JSON POST")
        response = await self.client.post(path, json= payload)
        await self._raise_api_error(ctx=ctx, response=response)
        return response.json()

    async def _get_json(
        self,
        *,
        ctx: Context,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        logger.bind(ctx=ctx, path=path, params=params).info("Calling Perplexity JSON GET")
        response = await self.client.get(path, params=params)
        await self._raise_api_error(ctx=ctx, response=response)
        return response.json()

    async def _stream_sse(
        self,
        *,
        ctx: Context,
        path: str,
        payload: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        logger.bind(ctx=ctx, path=path).info("Calling Perplexity streaming SSE endpoint")

        async with self.client.stream("POST", path, json=payload) as response:
            await self._raise_api_error(ctx=ctx, response=response)

            async for line in response.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue

                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue

                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    logger.bind(ctx=ctx, raw_line=data).warning(
                        "Skipping non-JSON SSE payload from Perplexity"
                    )

    # Sonar API
    async def create_chat_completion(
        self,
        *,
        ctx: Context,
        payload: dict[str, Any],
    ) -> Any:
        logger.bind(ctx=ctx, model=payload.get("model"), stream=payload.get("stream")).info(
            "Calling Perplexity /v1/sonar"
        )
        if payload.get("stream"):
            return self._stream_sse(ctx=ctx, path="/v1/sonar", payload=payload)
        return await self._post_json(ctx=ctx, path="/v1/sonar", payload=payload)

    async def create_async_chat_completion(
        self,
        *,
        ctx: Context,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        logger.bind(ctx=ctx, model=payload.get("model")).info(
            "Calling Perplexity /v1/async/sonar POST"
        )
        return await self._post_json(ctx=ctx, path="/v1/async/sonar", payload=payload)

    async def list_async_chat_completions(
        self,
        *,
        ctx: Context,
        next_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] | None = {"next_token": next_token} if next_token else None
        logger.bind(ctx=ctx, next_token=next_token).info(
            "Calling Perplexity /v1/async/sonar GET"
        )
        return await self._get_json(ctx=ctx, path="/v1/async/sonar", params=params)

    async def get_async_chat_completion(
        self,
        *,
        ctx: Context,
        request_id: str,
    ) -> dict[str, Any]:
        logger.bind(ctx=ctx, request_id=request_id).info(
            "Calling Perplexity /v1/async/sonar/{request_id} GET"
        )
        return await self._get_json(ctx=ctx, path=f"/v1/async/sonar/{request_id}")

    # Search API
    async def search_web(
        self,
        *,
        ctx: Context,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        logger.bind(
            ctx=ctx,
            query_count=len(payload["query"]) if isinstance(payload.get("query"), list) else 1,
            max_results=payload.get("max_results"),
        ).info("Calling Perplexity /search")
        return await self._post_json(ctx=ctx, path="/search", payload=payload)


def get_perplexity_conn(
    ctx: Context,
    user: UserORM,
    db: AsyncIOMotorDatabase,
    api_key: str,
) -> PerplexitySDKClient:
    return PerplexitySDKClient(
        ctx=ctx,
        user=user,
        db=db,
        api_key=api_key,
        timeout=3600,
    )