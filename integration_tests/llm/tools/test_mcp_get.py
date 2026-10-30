import pytest

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.openai.openai_responses.openai_responses import LlmResponsesOpenaiConfig
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.tool_factory import ToolFactory


@pytest.mark.asyncio
# @pytest.mark.skip(reason="Skipping test to test the other only.")
async def test_mcp_get(ctx, db, user, set_test_settings):
    tools = await ToolFactory.get_tools()
    tools.append("mcp_deepwiki")  # Add the MCP tool to the list
    assert tools is not None
    tool_defs = await ToolFactory.get_responses_function_tool_params(ctx=ctx, tool_names=tools)
    assert tool_defs is not None

    # Prepare the OpenAI Chat request to invoke our tool
    action_config = LlmResponsesOpenaiConfig(
        provider="openai",
        input=[
            {
                "role": "user",
                "content": "You are a helpful assistant, use mcp tool to answer a question.",
            }
        ],
        model="gpt-4.1",
        tools=tools,
    )

    # Create the ActionDoc for csv_xls_sheet_to_txt
    action_doc = ActionDoc(
        id="action_doc_mcp_1",
        user_id=user.id,
        name="mcp_tester",
        type=ActionType.text2text_llm_responses_openai,
        config=action_config.model_dump(exclude_none=True),
    )

    # Use a DataBlock carrying the URL
    action_input = DataBlock(
        text="What transport protocols does the 2025-03-26 version of the MCP spec (modelcontextprotocol/modelcontextprotocol) support?"
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


@pytest.mark.asyncio
@pytest.mark.skip(reason="Skipping test to test the other only.")
async def test_mcp_get_2(ctx, db, user, set_test_settings):
    tools = await ToolFactory.get_tools()
    tools.append("mcp_dmcp")  # Add the MCP tool to the list
    assert tools is not None
    tool_defs = await ToolFactory.get_responses_function_tool_params(ctx=ctx, tool_names=tools)
    assert tool_defs is not None

    # Prepare the OpenAI Chat request to invoke our tool
    action_config = LlmResponsesOpenaiConfig(
        provider="openai",
        input=[{"role": "user", "content": "You are a helpful assistant, use mcp tool."}],
        model="gpt-4.1",
        tools=tools,
    )

    # Create the ActionDoc for csv_xls_sheet_to_txt
    action_doc = ActionDoc(
        id="action_doc_mcp_1",
        user_id=user.id,
        name="mcp_tester",
        type=ActionType.text2text_llm_responses_openai,
        config=action_config.model_dump(exclude_none=True),
    )

    # Use a DataBlock carrying the URL
    action_input = DataBlock(text="Roll 2d4+1")

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
