import base64
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
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

class ActionText2textUserInput(ActionABC[DataBlock, DataBlock, DataBlocks, DataBlock, DataBlocks]):
    type = ActionType.text2text_user_input

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return DataBlock

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return DataBlock

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return DataBlocks

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    # def __init__(self, action_config: DataBlocks, user: UserORM | None = None):
    #     super().__init__(action_config=action_config, user=user)

    @staticmethod
    async def create_config(
        ctx: Context, app_state: AppState, config_create: DataBlock, user: UserORM, db: AsyncIOMotorDatabase
    ) -> DataBlocks:
        # data_blocks = DataBlocks(blocks=[])
        # if config_create.text:
        #     data_blocks.blocks.append(DataBlock(text=f"{config_create.text}"))
        # if config_create.files:
        #     for file in config_create.files:
        #         file_name = file.filename
        #         # file_text = await ActionText2textUserInput.b64_decode(file.content_base64)
        #         file_text = file.content
        #         data_blocks.blocks.append(DataBlock(text=f"file_name: {file_name}\nfile_data: {file_text}"))
        # return data_blocks
        data_blocks = DataBlocks(blocks=[config_create])
        return data_blocks

    @staticmethod
    async def update_config(
        ctx: Context,
        app_state: AppState,
        config: DataBlocks,
        config_update: DataBlock,
        user: UserORM,
        db: AsyncIOMotorDatabase,
    ) -> DataBlocks:
        data_blocks = await ActionText2textUserInput.create_config(
            ctx, app_state, config_update, user=user, db=db
        )
        return data_blocks

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks:
        # data_blocks = self.action_config
        # if action_input.text:
        #     data_blocks.blocks.append(DataBlock(text=f"{action_input.text}"))
        # if action_input.files:
        #     for file in action_input.files:
        #         file_name = file.filename
        #         # file_text = await ActionText2textUserInput.b64_decode(file.content_base64)
        #         file_text = file.content
        #         data_blocks.blocks.append(DataBlock(text=f"file_name: {file_name}\nfile_data: {file_text}"))
        # return data_blocks
        data_blocks = DataBlocks(blocks=[])
        if action_input.metadata.process_url_in_text:
            action_input.get_llm_prompt(ctx)
            action_input.process_url_in_text(ctx)
        for block in self.action_config.blocks:
            if block.text or block.urls or block.files or block.tools:
                data_blocks.blocks.append(block)
        data_blocks.blocks.append(action_input)
        return data_blocks

    @staticmethod
    async def b64_decode(b64_str: str) -> str:
        text = ""
        try:
            text = base64.b64decode(b64_str).decode()
        except Exception as e:
            logger.exception(str(e))
        return text

    # @staticmethod
    # async def read_file(file: UploadFile) -> str:
    #     try:
    #         content = await file.read()
    #         return str(content)
    #     except Exception as e:
    #         logger.error(str(e))

    async def run_tool(
        self, action_config: DataBlocks, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        pass
