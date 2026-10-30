from typing import Literal, Type

from loguru import logger
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks, TextObjs
from src.autobots.action.action_type.abc.ActionABC import (
    ActionABC,
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
    ActionInputType,
    ActionOutputType,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.duckduckgo.duckduckgo import get_duckduckgo
from src.autobots.conn.duckduckgo.duckduckgo_model import SearchTextParams
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class SearchWebConfig(BaseModel):
    search_type: Literal["web", "news"] = "web"
    search_params: SearchTextParams


class ActionText2TextSearchWeb(
    ActionABC[SearchWebConfig, SearchWebConfig, SearchWebConfig, DataBlock, DataBlocks]
):
    type = ActionType.text2text_search_web

    @staticmethod
    def get_description() -> str:
        return """
Description:
- Perform a DuckDuckGo web or news search and return the results as text blocks.
- Useful when you need external, up-to-date information from the public web or news.

When to use this tool:
- Use this tool when the user is asking about facts, current events, news, public websites, or general information that is not contained in the current context.
- Use "web" search for general web results (sites, blogs, docs, etc.).
- Use "news" search when the user explicitly asks for recent news, headlines, or coverage from news sources.

Input config:
{
  "search_type": "web" | "news",
  "search_params": {
    "keywords": "main search query terms",
    "place": "optional location filter, e.g. 'new york', 'mumbai'",
    "max_results": 10,
  }
}

Fields:
- search_type (string, required):
    - Allowed values:
        - "web": use standard DuckDuckGo web search.
        - "news": use DuckDuckGo news search.
    - Choose "web" for general information and "news" for recent news articles.

- search_params (object, required):
    - keywords (string, required):
        - The main search query.
        - At runtime, the tool will automatically append the text input (action_input.text)
          to these keywords, so you can provide a short, focused query here.
    - place (string, optional):
        - Free-text location name to bias results to a specific region or city.
        - Example: "washington", "california", "mumbai".
    - max_results (integer, optional):
        - Maximum number of results to request from DuckDuckGo.
        - If omitted, a reasonable default is used.
    - Other SearchTextParams fields (optional):
        - You can pass any other supported DuckDuckGo search parameters, such as
          time filters or safe-search settings, according to SearchTextParams.

Output:
- A DataBlocks object:
  {
    "blocks": [
      { "text": "<stringified search result 1>" },
      { "text": "<stringified search result 2>" },
      ...
    ]
  }
- Each block.text contains the serialized representation of a single search result.
- If an unexpected error occurs, an exception object is returned instead.
"""

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return SearchWebConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return SearchWebConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return SearchWebConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    # def __init__(self, action_config: SearchWebConfig, user: UserORM | None = None):
    #     super().__init__(action_config=action_config, user=user)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        data_blocks = DataBlocks(blocks=[])
        try:
            duckduckgo = get_duckduckgo()
            self.action_config.search_params.keywords = (
                f"{self.action_config.search_params.keywords} {action_input.text}"
            )
            search_res = []
            match self.action_config.search_type:
                case "news":
                    search_res = await duckduckgo.news(self.action_config.search_params)
                case "web":
                    search_res = await duckduckgo.search_text(self.action_config.search_params)
            for search in search_res:
                # text = f"title: {search.title}\nbody: {search.body}\nhref: {search.href}"
                text = str(search)
                data_blocks.blocks.append(DataBlock(text=text))
            return data_blocks
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    async def run_tool(
        self, action_config: SearchWebConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> TextObjs | Exception:
        try:
            action = ActionText2TextSearchWeb(
                action_config=action_config, user=self.user, db=self.db
            )
            action_input = DataBlock(text="")
            text_objs: DataBlocks | Exception = await action.run_action(
                ctx=ctx, app_state=app_state, action_input=action_input
            )
            return text_objs
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e
