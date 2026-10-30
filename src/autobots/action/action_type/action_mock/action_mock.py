from typing import Type

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import (
    ActionABC,
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
    ActionInputType,
    ActionOutputType,
)
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState


class MockAction(ActionABC[DataBlock, DataBlock, DataBlock, DataBlock, DataBlocks]):
    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return DataBlock

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return DataBlock

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return DataBlock

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    # def __init__(self, action_config: DataBlock, user: UserORM | None = None):
    #     super().__init__(action_config=action_config, user=user)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks:
        data = DataBlock(
            text=self.action_config.text + action_input.text,
            urls=self.action_config.urls + action_input.urls,
        )
        output = DataBlocks(blocks=[data])
        return output

    async def run_tool(
        self, action_config: DataBlock, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        pass
