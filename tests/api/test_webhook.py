import httpx
import pytest

from src.autobots.api import webhook as webhook_module
from src.autobots.api.webhook import Webhook


@pytest.fixture
def captured(monkeypatch):
    """Capture the request Webhook.send builds, without hitting the network.

    Patching the transport (not client.post) keeps httpx's json encoding in play, which
    is where the original bug lived.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(webhook_module.httpx, "AsyncClient", client_factory)
    return requests


@pytest.mark.asyncio
async def test_send_serializes_datetime(captured):
    from datetime import datetime

    await Webhook(url="https://example.com", track_id="runtest").send(
        {"id": "abc", "created_at": datetime(2026, 8, 14, 12, 0, 0)}
    )

    assert len(captured) == 1
    assert captured[0].read() == b'{"id":"abc","created_at":"2026-08-14T12:00:00"}'


@pytest.mark.parametrize(
    "url, track_id, expected",
    [
        ("https://example.com", "runtest", "https://example.com/runtest"),
        ("https://example.com", "", "https://example.com"),
        ("https://example.com/hooks", "", "https://example.com/hooks"),
        ("https://example.com/hooks", "runtest", "https://example.com/hooks/runtest"),
    ],
)
def test_target_url(url, track_id, expected):
    assert Webhook(url=url, track_id=track_id).target_url() == expected
