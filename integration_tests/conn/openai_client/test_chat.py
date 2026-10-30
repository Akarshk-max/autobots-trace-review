from typing import Literal

import pytest as pytest
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolParam,
    ChatCompletionUserMessageParam,
)

from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.openai.openai_chat.chat_model import ChatReq
from src.autobots.conn.openai.openai_function_definition_gen import OpenAIFunctionDefinitionGen
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "openai"])
@pytest.mark.parametrize(
    "model",
    [
        "model-router",
        "gpt-4.1-mini",
        "o4-mini",
        # "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o", "gpt-4o-0513", "gpt-4o-mini",
        # "o1-mini", "o3", "o3-mini", "o4-mini",
        "deepseek-r1",
    ],
)
async def test_openai_chat_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], model, set_test_settings
):
    if (provider == "openai" and model == "model-router") or (
        provider == "openai" and model == "deepseek-r1"
    ):
        pytest.skip("Model router not supported yet in Openai")

    msg0 = ChatCompletionUserMessageParam(role="user", content="You are a helpful assistant.")
    msg1 = ChatCompletionUserMessageParam(role="user", content="Most famous Mechanics law")
    params = ChatReq(messages=[msg0, msg1], model=model)
    if model == "o3":
        params.reasoning_effort = "medium"
    ctx = Context()

    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=provider)
    resp: ChatCompletion = await client.openai_chat.chat(ctx=ctx, app_state=AppState(), chat_req=params)

    assert "assistant" == resp.choices[0].message.role
    assert "Newton" in resp.choices[0].message.content


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "openai"])
# @pytest.mark.skip("Expense Related")
async def test_vision_chat_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    msg0 = ChatCompletionSystemMessageParam(role="system", content="You are a helpful assistant.")
    msg1 = ChatCompletionUserMessageParam(
        role="user",
        content=[
            {"type": "text", "text": "What’s in this image?"},
            {
                "type": "image_url",
                "image_url": {
                    "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/dd/Gfp-wisconsin-madison-the-nature-boardwalk.jpg/2560px-Gfp-wisconsin-madison-the-nature-boardwalk.jpg"
                },
            },
        ],
    )
    params = ChatReq(messages=[msg0, msg1], model="gpt-4o")
    ctx = Context()
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=provider)
    resp: ChatCompletion = await client.openai_chat.chat(ctx=ctx, app_state=AppState(), chat_req=params)

    assert "assistant" == resp.choices[0].message.role
    assert "boardwalk" in resp.choices[0].message.content


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "openai"])
@pytest.mark.skip("Not using this feature yet")
async def test_chat_stream_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    msg0 = ChatCompletionSystemMessageParam(role="system", content="You are a helpful assistant.")
    msg1 = ChatCompletionUserMessageParam(role="user", content="Most famous Mechanics law")
    params = ChatReq(messages=[msg0, msg1], model="gpt-4o-mini", stream=True)
    ctx = Context()
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=provider)
    stream = await client.openai_chat.chat(ctx=ctx, app_state=AppState(), chat_req=params)

    collected_words = ""
    async for part in stream:
        if (
            part.object == "chat.completion.chunk"
            and part.choices
            and len(part.choices) > 0
            and part.choices[0].delta.content
            and len(part.choices[0].delta.content) > 0
        ):
            collected_words = collected_words + part.choices[0].delta.content.lower()

    assert "newton" in collected_words


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_openai_function(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=provider)
    chat_completion_tool_param: ChatCompletionToolParam = (
        await OpenAIFunctionDefinitionGen.get_chat_completion_tool_param(
            client.openai_chat.chat, "LLM Chat"
        )
    )
    assert chat_completion_tool_param
