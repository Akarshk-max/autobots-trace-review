from typing import List, Type

import httpx
from loguru import logger
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.abc.ActionABC import (
    ActionABC,
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
    ActionInputType,
    ActionOutputType,
)
from src.autobots.action.action_type.action_multimodal.action_multimodal_google_search.action_multimodal_google_search_model import (
    ActionMultimodalGoogleSearchConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class SearchResult(BaseModel):
    title: str
    href: str
    body: str


class ActionRapidGoogleSearchOutput(BaseModel):
    result: List[SearchResult]


class ActionMultimodalGoogleSearchMain(
    ActionABC[
        ActionMultimodalGoogleSearchConfig,
        ActionMultimodalGoogleSearchConfig,
        ActionMultimodalGoogleSearchConfig,
        DataBlock,
        ActionRapidGoogleSearchOutput,
    ]
):
    type = ActionType.multimodal_google_search  # TODO: add type in ActionABC

    @staticmethod
    def get_description() -> str:
        return (
            "Browse the internet/Do a google search"
            "Performs a Google Custom Search (CSE) query and returns structured "
            "search results. The tool issues a search request using the configured "
            "query string and any runtime input appended to it, then parses the "
            "response into a list of results (title, link, snippet).\n\n"
            "Input:\n"
            "  {\n"
            '      "text": "query text",         # required in config\n'
            "  }\n\n"
            "  Runtime input (action input):\n"
            "    The tool accepts an optional text input which is appended to the "
            "Output:\n"
            "  An object containing a `result` list where each entry has:\n"
            "    - title: The result title\n"
            "    - href:  The result URL\n"
            "    - body:  A short snippet or description\n\n"
            "  If an unexpected error occurs, the exception is returned."
        )

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return ActionMultimodalGoogleSearchConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return ActionMultimodalGoogleSearchConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return ActionMultimodalGoogleSearchConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return ActionRapidGoogleSearchOutput

    async def _perform_google_search(self, query: str) -> dict:
        url = "https://www.googleapis.com/customsearch/v1"
        settings = SettingsProvider.sget()
        params = {"q": query, "key": settings.GOOGLE_CSE_API_KEY, "cx": settings.GOOGLE_CSE_CX}

        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> ActionRapidGoogleSearchOutput | Exception:
        try:
            query = self.action_config.q
            if action_input and action_input.text:
                query += f" {action_input.text}"

            data = await self._perform_google_search(query)
            results = []

            for item in data.get("items", []):
                results.append(
                    SearchResult(
                        title=item["title"], href=item["link"], body=item.get("snippet", "")
                    )
                )

            return ActionRapidGoogleSearchOutput(result=results)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in Google CSE search: {str(e)}")
            return e

    async def run_tool(
        self,
        action_config: ActionMultimodalGoogleSearchConfig,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> ActionRapidGoogleSearchOutput:
        action = ActionMultimodalGoogleSearchMain(
            action_config=action_config, user=self.user, db=self.db
        )
        input = DataBlock(text="")
        output = await action.run_action(ctx=ctx, app_state=app_state, action_input=input)
        return output
