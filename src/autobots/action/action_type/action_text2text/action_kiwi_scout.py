from typing import Type

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

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
from src.autobots.conn.kiwi_scout.output_model import KiwiScoutChat
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

class ActionKiwiScout(
    ActionABC[KiwiScoutChat, KiwiScoutChat, KiwiScoutChat, DataBlock, DataBlocks]
):
    type = ActionType.action_kiwi_scout

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return KiwiScoutChat

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return KiwiScoutChat

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return KiwiScoutChat

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    async def update_config(
        ctx: Context,
        app_state: AppState,
        config: KiwiScoutChat,
        config_update: KiwiScoutChat,
        user: UserORM,
        db: AsyncIOMotorDatabase,
    ) -> KiwiScoutChat:
        """
        Merge the existing config with the update and return a new KiwiScoutChat.
        If a field is not provided in the update, retain the original value.
        """
        try:
            # updated_config = KiwiScoutChat(
            #     success=config_update.success if config_update.success is not None else config.success,
            #     prompt=config_update.prompt or config.prompt,
            #     extracted_content=config_update.extracted_content or config.extracted_content,
            # )
            config_dict = config.model_dump()
            update_dict = config_update.model_dump(exclude_defaults=True)
            config_dict.update(update_dict)
            updated_config = KiwiScoutChat(**config_dict)
            return updated_config
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Failed to update config: {e}")
            raise

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks:
        """
        Run the browser use action with the provided input.
        :param ctx: Context for logging and tracking
        :param app_state: AppState for managing application state
        :param action_input: DataBlock containing text, URLs, files, and tools
        :return: DataBlocks containing processed data
        """

        try:
            extracted_content = self.action_config.extracted_content
            logger.bind(ctx=ctx).debug(f"Extracted content in config: {extracted_content}")
            output_blocks = [
                DataBlock(text=item) for item in extracted_content if isinstance(item, str)
            ]
            return DataBlocks(blocks=output_blocks)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Failed during run_action: {str(e)}")
            raise

    async def run_tool(
        self, action_config: ActionConfigType, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> ActionOutputType | Exception:
        pass
