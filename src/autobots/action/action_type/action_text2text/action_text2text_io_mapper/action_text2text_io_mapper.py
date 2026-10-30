from typing import Any, Dict, Type

from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlocks, TextObjs
from src.autobots.action.action_type.abc.ActionABC import (
    ActionABC,
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
    ActionInputType,
    ActionOutputType,
)
from src.autobots.action.action_type.action_text2text.action_text2text_io_mapper.io_mapper import (
    IOMapper,
    IOMapperConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class IOMapperInput(BaseModel):
    input_action_output: Dict[str, Any]
    input_action_type: ActionType | None = None


class ActionText2TextIOMapper(
    ActionABC[IOMapperConfig, IOMapperConfig, IOMapperConfig, IOMapperInput, TextObjs]
):
    type = ActionType.text2text_io_mapper

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return IOMapperConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return IOMapperConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return IOMapperConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return IOMapperInput

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return TextObjs

    # def __init__(self, action_config: IOMapperConfig, user: UserORM | None = None):
    #     super().__init__(action_config=action_config, user=user)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: IOMapperInput, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        if action_input.input_action_output:
            self.action_config.prev_action_output = action_input.input_action_output
        if action_input.input_action_type:
            self.action_config.prev_action_type = action_input.input_action_type
        resp = await IOMapper().map_to_output(ctx, self.action_config, user=self.user, db=self.db)
        return resp

    async def run_tool(
        self, action_config: IOMapperConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        pass
