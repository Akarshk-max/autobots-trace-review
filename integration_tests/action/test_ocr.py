from typing import Literal

import pytest
from openai.types.chat import ChatCompletionUserMessageParam

from src.autobots.action.action.action_doc_model import ActionDoc, ActionResult
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    LlmChatOpenaiConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.app_state import AppState
from src.autobots.llm.tools.tool_factory import ToolFactory

# @pytest.mark.asyncio
# async def test_gemini_conversation_happy_path_1(set_test_settings):
#     genai.configure(api_key=SettingsProvider.sget().GEMINI_API_KEY)
#     model = genai.GenerativeModel("gemini-1.5-flash")
#     response = model.generate_content("Write a story about a magic backpack.")
#     print(response.text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "gpt-4.1-mini",
    ],
)
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_action_llm_chat_openai_action_run_happy_path(
    ctx,
    db,
    user,
    model,
    provider: Literal["azure", "openai"],
    set_test_settings,
    register_tools_fixture,
):
    tools = await ToolFactory.get_tools()
    assert tools is not None
    tool_defs = await ToolFactory.get_chat_completion_tool_param(ctx=ctx, tool_names=tools)
    assert tool_defs is not None

    action_config = LlmChatOpenaiConfig(
        provider=provider,
        messages=[ChatCompletionUserMessageParam(role="user", content="Be a helpful assistant")],
        model=model,
        tools=tools,
    )
    action_doc = ActionDoc(
        id="action_doc_1",
        user_id=user.id,
        name="action_1",
        type=ActionType.text2text_llm_chat_openai,
        config=action_config.model_dump(exclude_none=True),
    )

    action_input = DataBlock(
        text='Extract data, use optical recognition tool https://app-myautobots-public-local.s3.us-west-2.amazonaws.com/files/test_user_id/ocr_test_page-0001.jpg, pass the url in chatreqgemini file_paths and give messages parts=["Extract file text verbatim and OCR scanned portions of the file"], role="system", NEVER SEND PARTS FILED EMPTY'
    )
    # action_input = {"text": "Learn about Runkai and Research competitors of Runkai"}
    action_run_obj_1 = await ActionFactory.run_action(
        ctx, AppState(), action_doc, action_input.model_dump(), user, db=db
    )
    assert action_run_obj_1.output_dict
    assert len(action_run_obj_1.config_dict.get("messages")) >= 3, (
        "Didnt get a response from OpenAI"
    )
    print(action_run_obj_1.output_dict)
    action_doc.config = action_run_obj_1.config_dict
    action_doc.input = action_run_obj_1.input_dict
    action_doc.output = action_run_obj_1.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))
