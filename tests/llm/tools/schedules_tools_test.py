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
async def test_webhook_tools(ctx, db, user, set_test_settings, register_tools_fixture):
    # Initialize context, DB, and available tools
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
                content="Be a helpful assistant.",
            )
        ],
        model="gpt-4.1-mini",
        tools=tools,
    )

    # Create the ActionDoc for csv_xls_sheet_to_txt
    action_doc = ActionDoc(
        id="action_doc_csv_1",
        user_id=user.id,
        name="schedule_tools_test_action",
        type=ActionType.text2text_llm_chat_openai,
        config=action_config.model_dump(exclude_none=True),
    )

    ## test for creating a webhook
    action_input = DataBlock(
        text="create a schedule with runnable id : 68efd77500193953f63413aa and input being text - 'Hello, World!', to run on an interval of 2 hrs. Use the interval trigger, also remember that convert the 2hrs to seconds and then it is passed as an integer value."
    )

    ## test for updating a webhook
    # action_input = DataBlock(
    #     text = "update the webhook with id : '0929f933-60d0-4748-940a-9fa24732aa17' to set runnable id to 'runnable_67890'"
    # )

    # ## test for get webhook
    # action_input = DataBlock(
    #     text = "get the webhook with id : 906886f3-f4a9-4b09-a999-43d90f783753"
    # )

    ## test for find webhook
    # action_input = DataBlock(
    #     text = "Find all the webhooks and list their ids to me."
    # )

    ## test for get webhook
    # action_input = DataBlock(
    #     text = "delete the webhook with id : 0929f933-60d0-4748-940a-9fa24732aa17"
    # )

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
