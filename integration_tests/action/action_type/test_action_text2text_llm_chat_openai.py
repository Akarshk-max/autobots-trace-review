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
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.tool_factory import ToolFactory


@pytest.mark.asyncio
# @pytest.mark.parametrize("model", ["gpt-4-32k", "gpt-4o", "gpt-4o-mini", "o1", "o1-mini", "o3-mini"])
# @pytest.mark.parametrize("model", ["gpt-4o", "gpt-4o-mini", "deepseek-r1"])
@pytest.mark.parametrize("model", ["gpt-4.1", "gpt-4.1-mini"])
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_action_llm_chat_openai_action_run_happy_path(
    ctx,
    app_state,
    db,
    user,
    provider: Literal["azure", "openai"],
    model,
    set_test_settings,
    register_tools_fixture,
):
    # tools = await ToolFactory.get_tools()
    tools = []
    # assert tools is not None
    # tool_defs = await ToolFactory.get_chat_completion_tool_param(ctx=ctx, tool_names=tools)
    # assert tool_defs is not None
    action_config = LlmChatOpenaiConfig(
        provider=provider,
        messages=[
            ChatCompletionUserMessageParam(
                role="user", content="Be a helpful assistant. Use tools to complete user request"
            )
        ],
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
    action_input = DataBlock(text="hi")
    # action_input = {"text": "Learn about Runkai and Research competitors of Runkai"}
    action_run_obj_1 = await ActionFactory.run_action(
        ctx, app_state, action_doc, action_input.model_dump(), user, db=db
    )
    assert action_run_obj_1.output_dict
    assert len(action_run_obj_1.config_dict.get("messages")) >= 3, (
        "Didnt get a response from OpenAI"
    )

    action_doc.config = action_run_obj_1.config_dict
    action_doc.input = action_run_obj_1.input_dict
    action_doc.output = action_run_obj_1.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))

    action_input = {"text": "Keep the same plot but make it funny"}
    app_state = AppState()
    action_run_obj_2 = await ActionFactory.run_action(ctx, app_state, action_doc, action_input, user, db=db)
    assert action_run_obj_2.output_dict
    assert len(action_run_obj_2.config_dict.get("messages")) >= 5, (
        "Didnt get 2nd response from OpenAI"
    )
    assert action_run_obj_2.config_dict.get("messages")[1].get(
        "content"
    ) == action_run_obj_1.input_dict.get("text")
    assert action_run_obj_1.output_dict.get("blocks")[0].get(
        "text"
    ) in action_run_obj_2.config_dict.get("messages")[-3].get("content")


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-4.1"])
@pytest.mark.parametrize("provider", ["openai", "azure"])
async def test_action_llm_chat_openai_action_run_with_file_happy_path2(
    ctx,
    db,
    user,
    provider: Literal["azure", "openai"],
    model,
    set_test_settings,
    register_tools_fixture,
):
    tools = await ToolFactory.get_tools()
    assert tools is not None
    tool_defs = await ToolFactory.get_chat_completion_tool_param(ctx=ctx, tool_names=tools)
    assert tool_defs is not None

    action_config = LlmChatOpenaiConfig(
        provider=provider,
        messages=[
            ChatCompletionUserMessageParam(
                role="user", content="Be a helpful assistant. Use tools to complete user request"
            )
        ],
        model=model,
        # model="o3-mini",
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
        text="Summarize doc",
        urls=[
            "https://courses.cs.umbc.edu/471/papers/turing.pdf"
            # "https://app-myautobots-dev.s3.us-west-2.amazonaws.com/files/9f6ab403-3d77-407e-912b-2438f09487d7/Brave.pdf"
        ],
    )
    # action_input = {"text": "Learn about Runkai and Research competitors of Runkai"}
    action_run_obj_1 = await ActionFactory.run_action(
        Context(), AppState(), action_doc, action_input.model_dump(), user, db=db
    )
    assert action_run_obj_1.output_dict
    assert len(action_run_obj_1.config_dict.get("messages")) >= 3
    assert "imitation game" in action_run_obj_1.config_dict.get("messages")[-1]["content"].lower()

    action_doc.config = action_run_obj_1.config_dict
    action_doc.input = action_run_obj_1.input_dict
    action_doc.output = action_run_obj_1.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))

    action_input = {"text": "Keep the same plot but make it funny"}
    app_state = AppState()
    action_run_obj_2 = await ActionFactory.run_action(
        ctx=ctx, app_state=app_state, action_doc=action_doc, action_input_dict=action_input, user=user, db=db
    )
    assert action_run_obj_2.output_dict
    assert (
        len(action_run_obj_2.config_dict.get("messages"))
        - len(action_run_obj_1.config_dict.get("messages"))
        >= 2
    )
    assert action_run_obj_1.output_dict.get("blocks")[0].get(
        "text"
    ) in action_run_obj_2.config_dict.get("messages")[-3].get("content")


@pytest.mark.asyncio
# @pytest.mark.parametrize("model", ["gpt-4-32k", "gpt-4o", "gpt-4o-mini", "o1", "o1-mini", "o3-mini"])
@pytest.mark.parametrize("model", ["o4-mini"])
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_action_llm_chat_openai_action_run_happy_path_2(
    ctx,
    db,
    user,
    provider: Literal["azure", "openai"],
    model,
    set_test_settings,
    register_tools_fixture,
):
    tools = await ToolFactory.get_tools()
    assert tools is not None
    tool_defs = await ToolFactory.get_chat_completion_tool_param(ctx=ctx, tool_names=tools)
    assert tool_defs is not None
    app_state = AppState()
    action_config = LlmChatOpenaiConfig(
        provider=provider,
        messages=[
            ChatCompletionUserMessageParam(
                role="user", content="Be a helpful assistant. Use tools to complete user request"
            )
        ],
        model=model,
        reasoning_effort="low",
        # model="o3-mini",
        tools=tools,
    )
    action_doc = ActionDoc(
        id="action_doc_1",
        user_id=user.id,
        name="action_1",
        type=ActionType.text2text_llm_chat_openai,
        config=action_config.model_dump(exclude_none=True),
    )

    action_input = DataBlock(text="Get latest Arsenal FC news")
    # action_input = {"text": "Learn about Runkai and Research competitors of Runkai"}
    action_run_obj_1 = await ActionFactory.run_action(
        Context(), app_state, action_doc, action_input.model_dump(), user, db=db
    )
    assert action_run_obj_1.output_dict
    assert len(action_run_obj_1.config_dict.get("messages")) >= 3, (
        "Didnt get a response from OpenAI"
    )

    action_doc.config = action_run_obj_1.config_dict
    action_doc.input = action_run_obj_1.input_dict
    action_doc.output = action_run_obj_1.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))

    action_input = {"text": "Keep the same plot but make it funny"}
    action_run_obj_2 = await ActionFactory.run_action(
        Context(), app_state, action_doc, action_input, user, db=db
    )
    assert action_run_obj_2.output_dict
    assert len(action_run_obj_2.config_dict.get("messages")) >= 5, (
        "Didnt get 2nd response from OpenAI"
    )
    assert action_run_obj_2.config_dict.get("messages")[1].get(
        "content"
    ) == action_run_obj_1.input_dict.get("text")
    assert action_run_obj_1.output_dict.get("blocks")[0].get(
        "text"
    ) in action_run_obj_2.config_dict.get("messages")[-3].get("content")
