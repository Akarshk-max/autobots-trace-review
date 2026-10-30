from typing import Literal

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_text2text.action_text2text_llm_responses_openai import (
    ActionText2TextLlmResponsesOpenai,
)
from src.autobots.conn.openai.openai_responses.openai_responses import LlmResponsesOpenaiConfig
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

class OpenAIAssistantToolConfig(BaseModel):
    provider: Literal["openai", "azure"]
    instructions: str
    urls: list[str] = []


async def openai_assistant(
    config: OpenAIAssistantToolConfig,
    ctx: Context,
    app_state: AppState,
    user: UserORM,
    db: AsyncIOMotorDatabase,
    notify: Notify | None = None,
) -> DataBlocks | Exception:
    """
    Description
    -----------
    Runs a general-purpose OpenAI assistant workflow using the text-to-text
    response pipeline. A minimal message configuration is created and supplied
    to the model along with tool capabilities. The instructions provided in
    `config` are passed into the action, and the model's output is collected
    into a `DataBlocks` object. If the underlying action reports or raises an
    error, it is logged with context and returned.

    Input
    -----
    config : dict
        Expected structure:
            {
                "instructions": "<string of instructions for the assistant>",
                "urls": [ "<optional list of reference URLs>" ]
            }

        instructions : str
            The main prompt or request that will be executed by the assistant.

        urls : list[str]
            Optional list of supporting URLs included with the request.

    Output
    ------
    DataBlocks | Exception
        Returns a `DataBlocks` instance containing model-generated blocks if the
        workflow completes normally. If the action returns an exception or an
        unexpected error occurs, the exception is logged and returned to the
        caller.
    """
    data_blocks = DataBlocks(blocks=[])
    try:
        action_config = LlmResponsesOpenaiConfig(
            provider="openai", tools=["file_search", "code_interpreter", "web_search"], input=[]
        )
        logger.bind(ctx=ctx).info(f"config is {action_config} and type is {type(action_config)}")
        action_config = await ActionText2TextLlmResponsesOpenai.create_config(
            ctx=ctx, app_state=app_state, config_create=action_config, user=user, db=db
        )
        action = ActionText2TextLlmResponsesOpenai(action_config=action_config, user=user, db=db)
        output = await action.run_action(
            ctx=ctx, action_input=DataBlock(text=config.instructions, urls=config.urls)
        )
        match output:
            case DataBlocks():
                data_blocks.blocks += output.blocks
            case Exception():
                data_block = DataBlock(text=str(output))
                data_blocks.blocks.append(data_block)
        return data_blocks
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in run tool of Openai assistant: {str(e)}")
        return e
