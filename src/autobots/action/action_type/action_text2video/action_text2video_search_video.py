from typing import Type

from loguru import logger
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import TextObj, TextObjs
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
from src.autobots.conn.duckduckgo.duckduckgo_model import SearchVideoParams
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class SearchVideoConfig(BaseModel):
    search_params: SearchVideoParams


class ActionText2VideoSearchVideo(
    ActionABC[SearchVideoConfig, SearchVideoConfig, SearchVideoConfig, TextObj, TextObjs]
):
    type = ActionType.text2video_search_video

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return SearchVideoConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return SearchVideoConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return SearchVideoConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return TextObj

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return TextObjs

    @staticmethod
    def get_description():
        return """
        Description
        -----------
        Performs a video search using DuckDuckGo based on the configured search
        parameters combined with any additional text provided at runtime. The
        query is sent to the DuckDuckGo client, and each matched video result is
        returned as a `TextObj`, preserving the raw string representation of the
        search result. If the search operation fails or an unexpected error
        occurs, the exception is returned.

        Input
        -----
        config : dict
            Expected structure:
                {
                    "search_params": {
                        "keywords": "<initial keywords>",
                        "region": "<optional region code>",
                        "safe_search": "<safe search setting>",
                        "max_results": <maximum results>
                    }
                }

            search_params.keywords : str
                Base search keywords. Runtime input text is appended to these
                keywords before performing the search.

            search_params.region : str | None
                Optional region or locale code to scope search results.

            search_params.safe_search : str | None
                Optional safe-search mode.

            search_params.max_results : int | None
                Maximum number of video results to return.

        Output
        ------
        dict
            Expected structure:
                {
                    "texts": [
                        {
                            "text": "<string representation of a video search result>"
                        },
                        ...
                    ]
                }

            Returns a list of `TextObj` entries, each containing a stringified
            representation of a DuckDuckGo video search result. If searching
            fails or any unexpected error is raised, the exception is returned
            instead.
        """

    # def __init__(self, action_config: TextObj, user: UserORM | None = None):
    #     super().__init__(action_config=action_config, user=user)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: TextObj, notify: Notify | None = None
    ) -> TextObjs | Exception:
        text_objs = TextObjs(texts=[])
        try:
            duckduckgo = get_duckduckgo()
            self.action_config.search_params.keywords = (
                f"{self.action_config.search_params.keywords} {action_input.text}"
            )
            search_res = await duckduckgo.search_videos(self.action_config.search_params)
            for search in search_res:
                # text = f"title: {search.title}\nbody: {search.body}\nhref: {search.href}"
                text = str(search)
                text_objs.texts.append(TextObj(text=text))
            return text_objs
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    async def run_tool(
        self,
        action_config: SearchVideoConfig,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> TextObjs:
        try:
            duckduckgo = get_duckduckgo()
            search_res = await duckduckgo.search_videos(action_config.search_params)

            text_objs = TextObjs(texts=[])
            for search in search_res:
                text_objs.texts.append(TextObj(text=str(search)))

            return text_objs

        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return TextObjs(texts=[TextObj(text=f"ERROR: {e}")])
