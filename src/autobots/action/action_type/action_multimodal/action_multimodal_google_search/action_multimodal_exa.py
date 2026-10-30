from typing import List, Type

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from src.autobots.action.action.common_action_models import DataBlock
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
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

class ExaSearchResult(BaseModel):
    title: str
    url: str
    snippet: str


class ActionMultimodalExaConfig(BaseModel):
    query: str
    num_results: int = Field(default=10, ge=1, le=100)


class ActionMultimodalExaOutput(BaseModel):
    organic_results: List[ExaSearchResult]


class ActionMultimodalExa(
    ActionABC[
        ActionMultimodalExaConfig,
        ActionMultimodalExaConfig,
        ActionMultimodalExaConfig,
        DataBlock,
        ActionMultimodalExaOutput,
    ]
):
    type = ActionType.multimodal_exa_search

    @staticmethod
    def get_description() -> str:
        return "Perform a search using the Exa API"

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return ActionMultimodalExaConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return ActionMultimodalExaConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return ActionMultimodalExaConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return ActionMultimodalExaOutput

    @staticmethod
    def get_description() -> str:  # noqa F811
        return """
        Description
        -----------
        Performs a multimodal search using the Exa API. The configured query is
        optionally extended with additional text provided at runtime, then sent
        to Exa to retrieve search results. The response is parsed into a list of
        structured results containing title, URL, and snippet text. If an error
        occurs while querying Exa or processing the results, the exception is
        returned.

        Input
        -----
        config : dict
            Expected structure:
                {
                    "query": "<base search query>",
                    "num_results": <number of results to return (1-100)>
                }

            query : str
                The main search query. Any text provided at runtime is appended
                to this query before the request is made.

            num_results : int
                Maximum number of search results to return. Must be between
                1 and 100.

        Output
        ------
        dict
            Expected structure:
                {
                    "organic_results": [
                        {
                            "title": "<result title>",
                            "url": "<result url>",
                            "snippet": "<short descriptive text>"
                        },
                        ...
                    ]
                }

            Returns a list of structured search results from Exa. If the Exa API
            request fails or an unexpected error occurs, an exception is returned
            instead.
        """

    def __init__(
        self,
        action_config: ActionMultimodalExaConfig,
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase | None = None,
    ):
        super().__init__(action_config=action_config, user=user, db=db)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> ActionMultimodalExaOutput | Exception:
        if action_input and action_input.text != "":
            self.action_config.query += " " + action_input.text
        try:
            client = await AppConn.get_exa(ctx=ctx, user=self.user, db=self.db)
            result = client.search(self.action_config.query, self.action_config.num_results)
            organic_results = [
                ExaSearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("snippet", ""),
                )
                for item in result.get("results", [])
            ]
            return ActionMultimodalExaOutput(organic_results=organic_results)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in Exa API search: {str(e)}")
            return e

    async def run_tool(
        self, action_config: ActionMultimodalExaConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> ActionMultimodalExaOutput | Exception:
        try:
            client = await AppConn.get_exa(ctx=ctx, user=self.user, db=self.db)
            result = client.search(action_config.query, action_config.num_results)
            organic_results = [
                ExaSearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("snippet", ""),
                )
                for item in result.get("results", [])
            ]
            return ActionMultimodalExaOutput(organic_results=organic_results)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in Exa API search: {str(e)}")
            return e
