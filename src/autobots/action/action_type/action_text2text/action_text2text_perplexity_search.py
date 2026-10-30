import json
from typing import List, Type

from loguru import logger
from pydantic import BaseModel, Field

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
from src.autobots.conn.perplexity.perplexity_search.perplexity_search import PerplexitySearch
from src.autobots.conn.perplexity.perplexity_search.search_model import (
    PerplexitySearchPage,
    PerplexitySearchReq,
)
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class PerplexitySearchActionConfig(BaseModel):
    query: str | List[str] | None = Field(
        default=None,
        description="Optional explicit search query. In run_action, action_input.text is preferred when present.",
    )
    country: str | None = None
    max_results: int | None = 10
    max_tokens: int | None = 10_000
    max_tokens_per_page: int | None = 4096

    search_language_filter: List[str] | None = None
    search_domain_filter: List[str] | None = None

    last_updated_after_filter: str | None = None
    last_updated_before_filter: str | None = None
    search_after_date_filter: str | None = None
    search_before_date_filter: str | None = None
    search_recency_filter: str | None = None


class ActionText2TextPerplexitySearch(
    ActionABC[
        PerplexitySearchActionConfig,
        PerplexitySearchActionConfig,
        PerplexitySearchActionConfig,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.text2text_perplexity_search

    @staticmethod
    def get_description() -> str:
        return """
Description:
- Call the Perplexity Search API and return search results as DataBlocks.
- In run_action, the incoming DataBlock is converted into a query if possible.
- In run_tool, the explicit config query is used.
- Produces one DataBlock per search result so downstream actions can consume the results naturally.

When to use this tool:
- Use when you want direct search result retrieval instead of a chat/reasoning answer.
- Use when you want multiple source results with title, snippet, date, and URL.
- Use when you want multi-query search in one request.

Input config:
{
  "query": "latest GPU launches",
  "country": "US",
  "max_results": 10,
  "search_language_filter": ["en"],
  "search_domain_filter": ["techcrunch.com"]
}

Input:
- DataBlock:
  {
    "text": "search query here",
    "urls": [...]
  }

Output:
- DataBlocks:
  {
    "blocks": [
      {
        "text": "<formatted search result>",
        "urls": ["<result url>"]
      }
    ]
  }
"""

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return PerplexitySearchActionConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return PerplexitySearchActionConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return PerplexitySearchActionConfig

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
        curr_config: PerplexitySearchActionConfig,
        prev_results: List[ActionResult] | None = None,
    ) -> PerplexitySearchActionConfig:
        return curr_config

    @staticmethod
    def _datablock_to_query(action_input: DataBlock) -> str | None:
        if getattr(action_input, "text", None):
            text = str(action_input.text).strip()
            if text:
                return text

        payload = action_input.model_dump(exclude_none=True)
        if payload:
            return json.dumps(payload, ensure_ascii=False)

        return None

    @staticmethod
    def _build_search_req(
        *,
        action_config: PerplexitySearchActionConfig,
        query_override: str | List[str] | None = None,
    ) -> PerplexitySearchReq:
        query = query_override if query_override is not None else action_config.query
        if query is None:
            raise ValueError("Search query is required")

        payload = action_config.model_dump(exclude_none=True)
        payload["query"] = query
        return PerplexitySearchReq.model_validate(payload)

    @staticmethod
    def _page_to_text(page: PerplexitySearchPage, query_label: str | None = None) -> str:
        parts: List[str] = []

        if query_label:
            parts.append(f"Query: {query_label}")

        if page.title:
            parts.append(f"Title: {page.title}")

        if page.snippet:
            parts.append(f"Snippet: {page.snippet}")

        if page.date:
            parts.append(f"Date: {page.date}")

        if page.last_updated:
            parts.append(f"Last updated: {page.last_updated}")

        if page.url:
            parts.append(f"URL: {page.url}")

        return "\n".join(parts).strip() or "Empty search result"

    @classmethod
    def _result_to_datablocks(
        cls,
        *,
        result,
        query: str | List[str],
    ) -> DataBlocks:
        blocks: List[DataBlock] = []

        if not result.results:
            return DataBlocks(blocks=[DataBlock(text="No search results found.", urls=[])])

        if result.is_multi_query:
            query_list = query if isinstance(query, list) else [str(query)]
            grouped_results = result.results

            for index, group in enumerate(grouped_results):
                query_label = query_list[index] if index < len(query_list) else f"query_{index + 1}"
                for page in group:
                    urls = [page.url] if page.url else []
                    blocks.append(
                        DataBlock(
                            text=cls._page_to_text(page, query_label=query_label),
                            urls=urls,
                        )
                    )
        else:
            flat_results = result.results
            query_label = query if isinstance(query, str) else None
            for page in flat_results:
                urls = [page.url] if page.url else []
                blocks.append(
                    DataBlock(
                        text=cls._page_to_text(page, query_label=query_label),
                        urls=urls,
                    )
                )

        if not blocks:
            blocks.append(DataBlock(text="No search results found.", urls=[]))

        return DataBlocks(blocks=blocks)

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        client = None
        try:
            client = await AppConn.get_perplexity(ctx=ctx, db=self.db, user=self.user)
            if isinstance(client, Exception):
                raise client

            query_override = self._datablock_to_query(action_input)
            search_req = self._build_search_req(
                action_config=self.action_config,
                query_override=query_override,
            )
            search_req.sanitize()

            logger.bind(
                ctx=ctx,
                query=str(search_req.query)[:100],
                max_results=search_req.max_results,
                country=search_req.country,
            ).info("Running Perplexity Search action")

            perplexity_search = PerplexitySearch(client)
            result = await perplexity_search.search(ctx=ctx, search_req=search_req)
            return self._result_to_datablocks(result=result, query=search_req.query)
        except Exception as e:
            logger.bind(ctx=ctx).exception("Error in Perplexity Search run_action: {}", str(e))
            return e
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    logger.bind(ctx=ctx).debug("Failed to close Perplexity client")

    async def run_tool(
        self,
        action_config: PerplexitySearchActionConfig,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        client = None
        try:
            client = await AppConn.get_perplexity(ctx=ctx, db=self.db, user=self.user)
            if isinstance(client, Exception):
                raise client

            search_req = self._build_search_req(
                action_config=action_config,
                query_override=None,
            )
            search_req.sanitize()

            logger.bind(
                ctx=ctx,
                query=str(search_req.query)[:100],
                max_results=search_req.max_results,
                country=search_req.country,
            ).info("Running Perplexity Search tool")

            perplexity_search = PerplexitySearch(client)
            result = await perplexity_search.search(ctx=ctx, search_req=search_req)
            return self._result_to_datablocks(result=result, query=search_req.query)
        except Exception as e:
            logger.bind(ctx=ctx).exception("Error in Perplexity Search run_tool: {}", str(e))
            return e
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    logger.bind(ctx=ctx).debug("Failed to close Perplexity client")