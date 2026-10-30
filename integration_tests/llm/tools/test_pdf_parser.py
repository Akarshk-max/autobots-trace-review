import pytest
from openai.types.chat import ChatCompletionUserMessageParam

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    LlmChatOpenaiConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.tool_factory import ToolFactory


@pytest.mark.asyncio
async def test_pdf_parser(ctx, db, user, set_test_settings):
    tools = await ToolFactory.get_tools()
    assert tools is not None
    tool_defs = await ToolFactory.get_chat_completion_tool_param(ctx=ctx, tool_names=tools)
    assert tool_defs is not None

    # Prepare the OpenAI Chat request to invoke our tool
    action_config = LlmChatOpenaiConfig(
        provider="openai",
        messages=[
            ChatCompletionUserMessageParam(
                role="user",
                content="Be a helpful assistant. Explain the content of the pdf given in the urls, using the tool, pdf_parser.",
            )
        ],
        model="gpt-4.1",
        tools=tools,
    )

    action_doc = ActionDoc(
        id="action_doc_csv_1",
        user_id=user.id,
        name="csv_xls_sheet_to_txt",
        type=ActionType.text2text_llm_chat_openai,
        config=action_config.model_dump(exclude_none=True),
    )

    # Use a DataBlock carrying the URL
    action_input = DataBlock(
        urls=[
            "https://app-myautobots-public-dev.s3.amazonaws.com/files/e29f3392-6124-40f5-8e24-fd6b5ebff875/materials-15-01147.pdf"
        ]
    )

    # Run the action
    action_run_obj = await ActionFactory.run_action(
        Context(), AppState(), action_doc, action_input.model_dump(), user, db=db
    )

    # Assertions: expect some text output
    assert action_run_obj.output_dict
    print("Output Dictionary:", action_run_obj.output_dict)
    final_block = action_run_obj.output_dict.get("blocks")[-1]
    print("Final Block:", final_block)
    text_output = final_block.get("text")
    assert isinstance(text_output, str)
    assert len(text_output) > 0
