from typing import Type

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
    ActionMultimodalGoogleSearchOutput,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.serp.serp_search_request import SerpGoogleSearchParams
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class SearchResult(BaseModel):
    title: str
    href: str
    body: str


class ActionMultimodalSerpGoogleSearch(
    ActionABC[
        ActionMultimodalGoogleSearchConfig,
        ActionMultimodalGoogleSearchConfig,
        ActionMultimodalGoogleSearchConfig,
        DataBlock,
        ActionMultimodalGoogleSearchOutput,
    ]
):
    type = ActionType.multimodal_serp_google_search  # TODO: add type in ActionABC

    @staticmethod
    def get_description() -> str:
        return "Do a google search"

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
        return ActionMultimodalGoogleSearchOutput

    # def __init__(self, action_config: ActionMultimodalGoogleSearchConfig, user: UserORM | None = None):
    #     super().__init__(action_config=action_config, user=user)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> ActionMultimodalGoogleSearchOutput | Exception:
        if action_input and action_input.text != "":
            self.action_config.q += action_input.text
        try:
            params = SerpGoogleSearchParams(**self.action_config.model_dump(exclude_none=True))
            result = await AppConn.get_google_serp(
                ctx=ctx, user=self.user, db=self.db
            ).serp_google_search.search(params)
            return ActionMultimodalGoogleSearchOutput.model_validate(result.model_dump())
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in Google Serp API search: {str(e)}")
            return e

    async def run_tool(
        self,
        action_config: ActionMultimodalGoogleSearchConfig,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> ActionMultimodalGoogleSearchOutput | Exception:
        try:
            params = SerpGoogleSearchParams(**action_config.model_dump(exclude_none=True))
            result = await AppConn.get_google_serp(
                ctx=ctx, user=self.user, db=self.db
            ).serp_google_search.search(params)
            return ActionMultimodalGoogleSearchOutput.model_validate(result.model_dump())
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in Google Serp API search: {str(e)}")
            return e
