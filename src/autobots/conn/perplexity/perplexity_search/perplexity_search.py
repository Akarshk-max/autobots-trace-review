from typing import Any

from loguru import logger

from src.autobots.conn.perplexity.perplexity_client import PerplexitySDKClient
from src.autobots.conn.perplexity.perplexity_search.search_model import (
    PerplexitySearchPage,
    PerplexitySearchReq,
)
from src.autobots.data_model.context import Context


class PerplexitySearchResult:
    def __init__(
        self,
        *,
        results: list[PerplexitySearchPage] | list[list[PerplexitySearchPage]],
        request_id: str,
        server_time: str | None,
        raw_response: dict[str, Any] | None = None,
    ) -> None:
        self.results = results
        self.id = request_id
        self.server_time = server_time
        self.raw_response = raw_response

    @property
    def is_multi_query(self) -> bool:
        return bool(self.results) and isinstance(self.results[0], list)

    @property
    def flat_results(self) -> list[PerplexitySearchPage]:
        if not self.results:
            return []

        if isinstance(self.results[0], list):
            flat: list[PerplexitySearchPage] = []
            for group in self.results:
                flat.extend(group)
            return flat

        return self.results


class PerplexitySearch:
    def __init__(self, sdk_client: PerplexitySDKClient):
        self.client = sdk_client

    @staticmethod
    def _parse_results(
        raw_results: Any,
    ) -> list[PerplexitySearchPage] | list[list[PerplexitySearchPage]]:
        if raw_results is None:
            return []

        if not isinstance(raw_results, list):
            raise ValueError("Perplexity Search response 'results' must be a list")

        if not raw_results:
            return []

        first = raw_results[0]

        # Single-query response: flat list of pages
        if isinstance(first, dict):
            return [PerplexitySearchPage.model_validate(item) for item in raw_results]

        # Multi-query response: grouped list of pages per query
        if isinstance(first, list):
            grouped: list[list[PerplexitySearchPage]] = []
            for group in raw_results:
                if not isinstance(group, list):
                    raise ValueError("Mixed result shapes are not supported in Search response")
                grouped.append([PerplexitySearchPage.model_validate(item) for item in group])
            return grouped

        raise ValueError("Unexpected Search response 'results' shape")

    def _build_payload(self, search_req: PerplexitySearchReq) -> dict[str, Any]:
        search_req.sanitize()
        return search_req.model_dump(exclude_none=True)

    async def search(
        self,
        *,
        ctx: Context,
        search_req: PerplexitySearchReq,
    ) -> PerplexitySearchResult:
        payload = self._build_payload(search_req)

        logger.bind(
            ctx=ctx,
            query_count=len(payload["query"]) if isinstance(payload.get("query"), list) else 1,
            country=payload.get("country"),
            max_results=payload.get("max_results"),
            max_tokens=payload.get("max_tokens"),
            max_tokens_per_page=payload.get("max_tokens_per_page"),
            search_language_filter=payload.get("search_language_filter"),
            search_domain_filter=payload.get("search_domain_filter"),
            search_recency_filter=payload.get("search_recency_filter"),
        ).info("Prepared Perplexity Search payload")

        raw_response = await self.client.search_web(ctx=ctx, payload=payload)
        results = self._parse_results(raw_response.get("results"))

        result = PerplexitySearchResult(
            results=results,
            request_id=raw_response["id"],
            server_time=raw_response.get("server_time"),
            raw_response=raw_response,
        )

        logger.bind(
            ctx=ctx,
            request_id=result.id,
            is_multi_query=result.is_multi_query,
            flat_result_count=len(result.flat_results),
            server_time=result.server_time,
        ).info("Completed Perplexity Search request")

        return result