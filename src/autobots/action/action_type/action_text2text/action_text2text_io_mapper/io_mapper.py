from typing import Any, Dict

from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_text2text.action_text2text_io_mapper.action_map_copy import (
    ACTION_MAP_COPY,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    ActionText2TextLlmChatOpenai,
    LlmChatOpenaiConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


class IOMapperConfig(BaseModel):
    chat_config: LlmChatOpenaiConfig
    prev_action_output: Dict[str, Any]
    prev_action_type: ActionType | None = None
    next_action_type: ActionType


class IOMapper:
    async def map_to_output(
        self, ctx: Context, mapper_config: IOMapperConfig, db: AsyncIOMotorDatabase, user: UserORM
    ) -> DataBlocks | Exception:
        instruction = await self._build_instruction(mapper_config)

        # mapper_config.chat_config.chat_req.messages.append({
        #     "role": "user", "content": instruction
        # })
        chat_action = ActionText2TextLlmChatOpenai(
            action_config=mapper_config.chat_config, db=db, user=user
        )
        action_input = DataBlock(text=instruction)
        resp = await chat_action.run_action(ctx, action_input)
        return resp

    async def _build_instruction(self, mapper_config: IOMapperConfig) -> str:
        instruction_start = "\nGiven the Input, the input json schema and the output json schema, create a json of the output.\n"
        input_context = await self._build_data_context(mapper_config)
        mapping_context = await self._build_mapping_context(mapper_config)
        instruction_end = "\nNote: only respond with output json.\n"
        instruction = f"{instruction_start}{input_context}{mapping_context}{instruction_end}"
        return instruction

    async def _build_data_context(self, mapper_config: IOMapperConfig) -> str:
        context = f"Input data is:\n{mapper_config.prev_action_output}"
        return context

    async def _build_mapping_context(self, mapper_config: IOMapperConfig) -> str:
        context = ""
        if mapper_config.prev_action_type:
            # USING ACTION_MAP_COPY TO SAVE FROM CIRCULAR IMPORT
            input_action_output_data_type = (
                ACTION_MAP_COPY.get(mapper_config.prev_action_type)
                .get_output_type()
                .model_json_schema()
            )
            context = context + f"\nInput datatype is:\n{input_action_output_data_type}\n"
        if mapper_config.next_action_type:
            # USING ACTION_MAP_COPY TO SAVE FROM CIRCULAR IMPORT
            output_action_data_type = (
                ACTION_MAP_COPY.get(mapper_config.next_action_type)
                .get_input_type()
                .model_json_schema()
            )
            context = context + f"\nOutput datatype is:\n{output_action_data_type}"
        return context
