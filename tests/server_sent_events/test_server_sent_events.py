import asyncio

import pytest
from fastapi import HTTPException

from src.autobots.api.v1.api_server_sent_events import api_server_sent_events as api_sse
from src.autobots.api.v1.api_server_sent_events.api_server_sent_events import sse_frame
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events import server_sent_events as sse
from src.autobots.server_sent_events.server_sent_events import Notify, NotifyPayload

BURST = 100


class FakeRedis:
    """Records publishes in arrival order, with a hop so ordering is observable."""

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


async def _settle(notify: Notify) -> None:
    while not notify._queue.empty():
        await asyncio.sleep(0.01)
    await asyncio.sleep(sse.COALESCE_INTERVAL_SECONDS * 2)


@pytest.mark.asyncio
async def test_delta_messages_all_arrive_in_order(fake_redis):
    """Deltas add to each other, so none may be dropped or reordered."""
    ctx = Context()
    notify = Notify("topic-delta")

    for i in range(BURST):
        notify(ctx, f"chunk-{i}")
    await _settle(notify)

    assert fake_redis.published == [("topic-delta", f"chunk-{i}") for i in range(BURST)]


@pytest.mark.asyncio
async def test_snapshot_messages_coalesce_to_the_newest(fake_redis):
    """Snapshots replace each other, so superseded ones are dropped."""
    ctx = Context()
    notify = Notify("topic-snapshot", payload=NotifyPayload.snapshot)

    for i in range(BURST):
        notify(ctx, f"answer so far {i}")
    await _settle(notify)

    assert len(fake_redis.published) < BURST
    assert fake_redis.published[-1] == ("topic-snapshot", f"answer so far {BURST - 1}")


@pytest.mark.asyncio
async def test_snapshot_delivers_the_last_message_of_every_burst(fake_redis):
    """Coalescing must never swallow the final state of a stream."""
    ctx = Context()
    notify = Notify("topic-bursts", payload=NotifyPayload.snapshot)

    for burst in range(3):
        for i in range(BURST):
            notify(ctx, f"burst-{burst}-{i}")
        await _settle(notify)

    tails = [message for _topic, message in fake_redis.published]
    for burst in range(3):
        assert f"burst-{burst}-{BURST - 1}" in tails


@pytest.mark.asyncio
async def test_pump_stops_when_idle_and_restarts_on_the_next_message(fake_redis, monkeypatch):
    monkeypatch.setattr(sse, "IDLE_SHUTDOWN_SECONDS", 0.05)
    ctx = Context()
    notify = Notify("topic-idle")

    notify(ctx, "first")
    await _settle(notify)
    await asyncio.sleep(0.15)
    assert notify._pump.done()

    notify(ctx, "second")
    await _settle(notify)

    assert fake_redis.published == [("topic-idle", "first"), ("topic-idle", "second")]


@pytest.mark.asyncio
async def test_oversized_message_is_truncated_to_the_limit(fake_redis):
    ctx = Context()
    notify = Notify("topic-large")

    notify(ctx, "x" * (sse.MESSAGE_CHAR_LIMIT + 500))
    await _settle(notify)

    _topic, message = fake_redis.published[0]
    assert message.startswith("...")
    assert len(message) == sse.MESSAGE_CHAR_LIMIT + 3


def test_sse_frame_prefixes_every_line_of_the_payload():
    """An unprefixed line would end the event early and drop the rest."""
    assert sse_frame("one") == "data: one\n\n"
    assert sse_frame("one\ntwo") == "data: one\ndata: two\n\n"
    assert sse_frame("para\n\nbreak") == "data: para\ndata: \ndata: break\n\n"


class FakeWebSocket:
    def __init__(self, query_params: dict | None = None, messages: list | None = None):
        self.query_params = query_params or {}
        self._messages = list(messages or [])

    async def receive(self):
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(3600)


class FakeAuthProvider:
    def __init__(self, accepted_token: str):
        self.accepted_token = accepted_token

    async def verify_token_offline(self, token, audience="authenticated"):
        if token != self.accepted_token:
            raise HTTPException(status_code=403, detail="Could not validate credentials")
        return "user-response"


@pytest.fixture
def auth_provider(monkeypatch):
    provider = FakeAuthProvider(accepted_token="good-token")
    monkeypatch.setattr(api_sse, "get_auth_provider", lambda: provider)
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query_params",
    [{}, {"token": "bad-token"}, {"Authorization": "Bearer bad-token"}],
)
async def test_websocket_without_a_valid_token_is_not_authenticated(auth_provider, query_params):
    assert await api_sse.authenticate_websocket(FakeWebSocket(query_params)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query_params",
    [{"token": "good-token"}, {"Authorization": "Bearer good-token"}],
)
async def test_websocket_with_a_valid_token_is_authenticated(auth_provider, query_params):
    assert await api_sse.authenticate_websocket(FakeWebSocket(query_params)) == "user-response"


@pytest.mark.asyncio
async def test_wait_for_disconnect_returns_when_the_client_goes_away():
    websocket = FakeWebSocket(
        messages=[{"type": "websocket.receive"}, {"type": "websocket.disconnect"}]
    )
    await asyncio.wait_for(api_sse.wait_for_disconnect(websocket), timeout=1)
