import asyncio
import time
from collections import deque
from types import SimpleNamespace

import pytest

from src.autobots.api.v1.api_server_sent_events import api_server_sent_events as sse
from src.autobots.auth import security

BACKLOG = 300
SLEEP_INTERVAL = 0.1


class FakePubSub:
    """Stands in for redis.asyncio PubSub with a backlog already queued.

    Mirrors the contract that caused the bug: get_message() returns immediately
    with None when nothing is pending, so an unconditional sleep in the read loop
    becomes a hard per-message rate cap.
    """

    def __init__(self, messages):
        self.queue = deque(messages)

    async def subscribe(self, topic):
        return None

    async def get_message(self, ignore_subscribe_messages=False, timeout=0.0):
        await asyncio.sleep(0)
        if self.queue:
            return {"type": "message", "data": self.queue.popleft()}
        return None

    async def close(self):
        return None


class FakeRedis:
    def __init__(self, pubsub):
        self._pubsub = pubsub

    def pubsub(self):
        return self._pubsub


@pytest.fixture
def queued_backlog(monkeypatch):
    """Point the endpoints at a pubsub holding BACKLOG ready-to-read messages."""
    pubsub = FakePubSub([f"token-{i}".encode() for i in range(BACKLOG)])

    async def get_async_redis():
        return FakeRedis(pubsub)

    async def allow_any_topic(topic, user_res, db):
        return None

    monkeypatch.setattr(sse, "get_async_redis", get_async_redis)
    # These exercise the read loop, not authorisation.
    monkeypatch.setattr(sse, "assert_topic_readable", allow_any_topic)
    return pubsub


async def _drain(endpoint):
    request = SimpleNamespace(state=SimpleNamespace(context=None))
    response = await endpoint(request, "topic-under-test", user_res=None, db=None)

    received = 0
    started = time.perf_counter()
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8")
        if chunk.startswith(":") or '"type": "ping"' in chunk:
            continue  # heartbeat, not a payload frame
        received += 1
        if received >= BACKLOG:
            break
    return received, time.perf_counter() - started


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [sse.stream_sse, sse.streaming_ndjson])
async def test_backlog_drains_without_per_message_sleep(queued_backlog, endpoint):
    """A queued backlog must not be throttled to one message per sleep interval.

    Before the fix the loop slept 0.1s on every iteration, so BACKLOG messages
    took BACKLOG * 0.1s to deliver no matter how many were already waiting.
    """
    received, elapsed = await asyncio.wait_for(_drain(endpoint), timeout=60)

    assert received == BACKLOG
    assert elapsed < BACKLOG * SLEEP_INTERVAL / 10, (
        f"{BACKLOG} queued messages took {elapsed:.2f}s to drain; "
        "the read loop is still sleeping between messages"
    )


def _dependency_calls(route):
    """Every dependency callable the route resolves, at any depth."""
    seen = []
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        seen.append(dep.call)
        stack.extend(dep.dependencies)
    return seen


def test_test_trigger_is_admin_only():
    """Publishing test frames must not be reachable without an admin token.

    This one took no dependency at all, so anyone could publish to any topic —
    and a topic is a run id, which is an ObjectId rather than a secret. Asserted
    on the route rather than through a request because the endpoints here are
    called directly, which bypasses dependency resolution entirely.
    """
    (route,) = [
        r for r in sse.router.routes if r.path.endswith("/test_trigger/{topic}")
    ]

    assert security.get_online_admin_user_from_access_token in _dependency_calls(route)
