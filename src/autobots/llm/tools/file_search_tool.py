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


async def file_search_tool(
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
    Performs a file-search operation using OpenAI's file_search tool. The tool
    takes instructions and a list of file URLs, constructs an OpenAI chat
    configuration with the file_search tool enabled, executes the request, and
    returns the model output as DataBlocks. If an unexpected error occurs, the
    exception is returned.

    Input
    -----
    config : dict
        A dictionary of arguments supplied by the caller.
        Expected structure:
            {
                "instructions": "Search these files for X",
                "urls": [
                    "https://example.com/file1.pdf",
                    "https://example.com/file2.txt",
                    ...
                ]
            }

        instructions : str
            The natural-language instruction that describes what the file search
            tool should do. This is passed directly into the model.

        urls : list[str]
            A list of URLs pointing to files that should be searched. These
            URLs are attached to the model input and become available to the
            file_search tool during execution.

    Output
    ------
    DataBlocks
        A DataBlocks object containing the model's response. This may include
        extracted content, search results, summaries, or structured outputs
        depending on the instruction.

        In case of an unexpected error, the exception object is returned.
    """
    data_blocks = DataBlocks(blocks=[])
    try:
        action_config = LlmResponsesOpenaiConfig(provider="openai", tools=["file_search"], input=[])
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
