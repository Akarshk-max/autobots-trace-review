from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_text2text.action_text2text_llm_responses_openai import (
    ActionText2TextLlmResponsesOpenai,
)
from src.autobots.conn.openai.openai_responses.openai_responses import LlmResponsesOpenaiConfig
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.openai_assistant_tool import OpenAIAssistantToolConfig
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

async def coding_assistant(
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
    Executes an OpenAI-powered coding assistant workflow. A lightweight message
    configuration is created and passed into the text-to-text action pipeline,
    which sends the provided instructions to the model along with tool
    capabilities. The model output is then normalized into a `DataBlocks`
    container. If the action returns an exception or raises one internally, the
    error is captured, logged with context, and returned to the caller.

    Input
    -----
    config : dict
        Expected structure:
            {
                "provider" : "openai",
                "instructions": "<string of instructions for the assistant>",
                "urls": [ "<optional list of reference URLs>" ]
            }

        provider : str
            choose between openai or azure as a provider
        instructions : str
            The main task description or prompt that will be sent to the model.

        urls : list[str]
            Optional list of external URLs passed through as additional input.

    Output
    ------
    DataBlocks | Exception
        Returns a `DataBlocks` instance containing the model's output blocks if
        the action completes successfully. If the action returns or raises an
        exception, that exception is logged and propagated back to the caller.

    """

    data_blocks = DataBlocks(blocks=[])
    try:
        action_config = LlmResponsesOpenaiConfig(
            provider="openai", tools=["code_interpreter"], input=[]
        )
        action_config = await ActionText2TextLlmResponsesOpenai.create_config(
            ctx=ctx, app_state=app_state, config_create=action_config, user=user, db=db
        )
        action = ActionText2TextLlmResponsesOpenai(action_config=action_config, user=user, db=db)
        output = await action.run_action(
            ctx=ctx, app_state=app_state, action_input=DataBlock(text=config.instructions, urls=config.urls)
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
