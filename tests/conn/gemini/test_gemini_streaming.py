import asyncio
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from src.autobots.conn.gemini.gemini_chat import GeminiChatRuntime
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events import server_sent_events as sse
from src.autobots.server_sent_events.server_sent_events import Notify

RUN_ID = "run-id"
TEXT_TOPIC = f"{RUN_ID}-text"


class FakeChunk(BaseModel):
    text: str | None = None
    candidates: list[dict] = []


class FakeGeminiClient:
    def __init__(self, chunks):
        self.chunks = chunks
        self.stream_calls = 0
        self.non_stream_calls = 0

    async def generate_content(self, **kwargs):
        self.non_stream_calls += 1
        return {"candidates": []}

    async def generate_content_stream(self, **kwargs):
        self.stream_calls += 1

        async def chunk_iterator():
            for chunk in self.chunks:
                yield chunk

        return chunk_iterator()


class FakeRedis:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, topic, message):
        await asyncio.sleep(0)
        self.published.append((topic, message))


@pytest.fixture
def fake_redis(monkeypatch):
    redis = FakeRedis()

    async def get_async_redis():
        return redis

    monkeypatch.setattr(sse, "get_async_redis", get_async_redis)
    return redis


def _text_chunk(text: str) -> FakeChunk:
    return FakeChunk(
        text=text, candidates=[{"content": {"role": "model", "parts": [{"text": text}]}}]
    )


def _tool_chunk() -> FakeChunk:
    call = {"id": "call-1", "name": "bash", "args": {"command": "ls -la"}}
    return FakeChunk(
        candidates=[{"content": {"role": "model", "parts": [{"function_call": call}]}}]
    )


async def _run(chunks, *, stream: bool):
    runtime = object.__new__(GeminiChatRuntime)
    client = FakeGeminiClient(chunks)
    runtime.gemini_client = client
    notify = Notify(topic=RUN_ID)

    response = await runtime._create_or_stream_message(
        ctx=Context(),
        config=SimpleNamespace(model="gemini-2.5-flash", stream=stream),
        contents=[],
        generation_config=None,
        notify=notify,
    )
    while not notify._queue.empty():
        await asyncio.sleep(0.01)
    await asyncio.sleep(sse.COALESCE_INTERVAL_SECONDS * 2)
    return client, response


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_the_config_stream_flag_selects_the_api_call(fake_redis, stream):
    client, _ = await _run([_text_chunk("Hello")], stream=stream)

    assert client.stream_calls == (1 if stream else 0)
    assert client.non_stream_calls == (0 if stream else 1)


@pytest.mark.asyncio
async def test_streamed_text_is_published_as_a_snapshot_to_the_text_topic(fake_redis):
    await _run([_text_chunk("Hello "), _text_chunk("world")], stream=True)

    text_messages = [m for topic, m in fake_redis.published if topic == TEXT_TOPIC]
    assert text_messages
    assert text_messages[-1] == "Hello world"


@pytest.mark.asyncio
async def test_streamed_tool_calls_use_the_shared_format(fake_redis):
    await _run([_text_chunk("Checking"), _tool_chunk()], stream=True)

    status_messages = [m for topic, m in fake_redis.published if topic == RUN_ID]
    tool_messages = [m for m in status_messages if m.startswith("Calling tool:")]
    assert tool_messages
    assert "Calling tool: bash" in tool_messages[-1]
    assert "- command: ls -la" in tool_messages[-1]


@pytest.mark.asyncio
async def test_stream_response_carries_text_and_tool_calls(fake_redis):
    _client, response = await _run([_text_chunk("Hi"), _tool_chunk()], stream=True)

    parts = response["candidates"][0]["content"]["parts"]
    assert {"text": "Hi"} in parts
    assert any("function_call" in part for part in parts)
