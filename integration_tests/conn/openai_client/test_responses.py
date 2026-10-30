from typing import Literal

import pytest
from openai.types.responses import EasyInputMessageParam, Response
from openai.types.responses.tool_param import (
    CodeInterpreter,
    CodeInterpreterContainerCodeInterpreterToolAuto,
)

from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.openai.openai_responses.openai_responses import LlmResponsesOpenaiConfig
from src.autobots.data_model.app_state import AppState
from src.autobots.llm.tools.tool_factory import ToolFactory


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_responses_function_tool_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    # tools = await ToolFactory.get_tools()
    tools = ["google_search_main"]
    assert tools is not None
    tool_defs = await ToolFactory.get_responses_function_tool_params(ctx=ctx, tool_names=tools)
    assert tool_defs is not None

    responses_req = LlmResponsesOpenaiConfig(
        provider=provider,
        model="o4-mini",
        input=[
            EasyInputMessageParam(
                role="user",
                content="Give me detailed latest Indian test cricket news. Use google search tool",
                type="message",
            ),
        ],
        tools=tool_defs,
    )

    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=provider)
    openai_responses = client.openai_responses
    resp: Response = await openai_responses.create(
        responses_req=responses_req, user=user, ctx=ctx, app_state=AppState(), db=db
    )

    assert resp is not None

    is_tool_called = 0
    for input_param in responses_req.input:
        if (
            input_param.get("type") == "function_call"
            or input_param.get("type") == "function_call_output"
        ):
            is_tool_called += 1
    assert is_tool_called > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai"])
async def test_responses_generate_file_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    # tools = await ToolFactory.get_tools()
    code_interpreter_tool = CodeInterpreter(
        type="code_interpreter",
        container=CodeInterpreterContainerCodeInterpreterToolAuto(
            type="auto",
        ),
    )
    # tools = ["google_search_main"]
    # assert tools is not None
    # tool_defs = await ToolFactory.get_responses_function_tool_params(ctx=ctx, tool_names=tools)
    tool_defs = [code_interpreter_tool]
    assert tool_defs is not None

    responses_req = LlmResponsesOpenaiConfig(
        provider=provider,
        model="gpt-4.1",
        input=[
            EasyInputMessageParam(
                role="user",
                content="Use code_interpreter tool and Create a pdf with sample blog on San Francisco",
                type="message",
            ),
        ],
        tools=tool_defs,
    )

    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=provider)
    openai_responses = client.openai_responses
    resp: Response = await openai_responses.create(
        responses_req=responses_req, user=user, ctx=ctx, app_state=AppState(), db=db
    )

    assert resp is not None

    is_tool_called = 0
    for input_param in responses_req.input:
        if input_param.get("type") == "code_interpreter_call":
            is_tool_called += 1
    assert is_tool_called > 0, "Code Interpreter tool not called"
