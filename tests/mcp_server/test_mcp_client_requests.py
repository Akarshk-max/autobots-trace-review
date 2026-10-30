"""Request-shape tests for the MCP server's APIClient.

These endpoints declare more than one body param (e.g. `input` plus `webhook`),
so FastAPI embeds them by name. Sending the payload as the bare body makes the
API reject every call with 422. That bug is invisible to type checkers and to
any test that mocks at the APIClient level, so these tests assert the exact
JSON that goes over the wire.
"""

import pytest

from src.autobots.mcp_server.client import APIClient, APIClientError


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.text = "fake"

    def json(self):
        return self._payload


class _Recorder:
    """Captures the kwargs of the single httpx request the client makes."""

    def __init__(self, response=None):
        self.calls = []
        self._response = response or _FakeResponse()

    def install(self, monkeypatch):
        recorder = self

        class _FakeAsyncClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def request(self, method, url, **kwargs):
                recorder.calls.append({"method": method, "url": url, **kwargs})
                return recorder._response

        monkeypatch.setattr("src.autobots.mcp_server.client.httpx.AsyncClient", _FakeAsyncClient)
        return self

    @property
    def last(self):
        return self.calls[-1]


async def _token():
    return "test-token"


@pytest.fixture
def client():
    return APIClient(base_url="http://testserver", get_token=_token)


@pytest.mark.asyncio
async def test_run_action_embeds_input_under_input_key(client, monkeypatch):
    rec = _Recorder().install(monkeypatch)
    await client.run_action("act1", {"text": "Hi"})
    assert rec.last["json"] == {"input": {"text": "Hi"}}
    assert rec.last["url"] == "http://testserver/v1/actions/act1/async_run"


@pytest.mark.asyncio
async def test_run_action_graph_embeds_input_under_input_key(client, monkeypatch):
    rec = _Recorder().install(monkeypatch)
    await client.run_action_graph("g1", {"text": "Hi"})
    assert rec.last["json"] == {"input": {"text": "Hi"}}


@pytest.mark.asyncio
async def test_run_action_team_embeds_input_under_input_key(client, monkeypatch):
    rec = _Recorder().install(monkeypatch)
    await client.run_action_team("t1", {"text": "Hi"})
    assert rec.last["json"] == {"input": {"text": "Hi"}}


@pytest.mark.asyncio
async def test_run_tool_embeds_args_under_tool_args_key(client, monkeypatch):
    rec = _Recorder().install(monkeypatch)
    await client.run_tool("get_date_time", {"tz": "UTC"})
    assert rec.last["json"] == {"tool_args": {"tz": "UTC"}}
    assert rec.last["url"] == "http://testserver/v1/tools/get_date_time/run"


@pytest.mark.asyncio
async def test_get_token_is_awaited_and_sent_as_bearer(client, monkeypatch):
    """A sync callable here raises 'object str can't be used in await expression'."""
    rec = _Recorder().install(monkeypatch)
    await client.list_tools()
    assert rec.last["headers"]["Authorization"] == "Bearer test-token"


@pytest.mark.parametrize(
    "configured",
    [
        "https://dev.api.myautobots.com",
        "https://dev.api.myautobots.com/",
        "https://dev.api.myautobots.com///",
    ],
)
@pytest.mark.asyncio
async def test_trailing_slash_in_base_url_does_not_double_up(configured, monkeypatch):
    """APP_BASE_URL is env-supplied and may carry a trailing slash."""
    rec = _Recorder().install(monkeypatch)
    await APIClient(base_url=configured, get_token=_token).list_tools()
    assert rec.last["url"] == "https://dev.api.myautobots.com/v1/tools/"


@pytest.mark.asyncio
async def test_non_2xx_raises_api_client_error(client, monkeypatch):
    rec = _Recorder(_FakeResponse(404, {"detail": "Action result not found"}))
    rec.install(monkeypatch)
    with pytest.raises(APIClientError) as exc:
        await client.get_action_result("missing")
    assert exc.value.status_code == 404
    assert "not found" in exc.value.detail


# --- pagination -------------------------------------------------------------
# The search handler used to drop `offset` on the floor, so every page request
# silently returned page 0. Asserted end-to-end through dispatch() because the
# bug lived in the handler, not the client.


@pytest.mark.asyncio
async def test_search_actions_forwards_offset(client, monkeypatch):
    rec = _Recorder().install(monkeypatch)
    await client.search_actions("logo", limit=5, offset=20)
    assert rec.last["params"] == {"q": "logo", "limit": 5, "offset": 20}


@pytest.mark.asyncio
async def test_search_actions_handler_forwards_offset(monkeypatch):
    from src.autobots.mcp_server import middleware
    from src.autobots.mcp_server.handlers import dispatch

    seen = {}

    class _Client:
        async def search_actions(self, query, limit=10, offset=0):
            seen.update(query=query, limit=limit, offset=offset)
            return {"ok": True}

    token = middleware._api_client_ctx.set(_Client())
    try:
        await dispatch("autobots_search_actions", {"query": "logo", "limit": 5, "offset": 20})
    finally:
        middleware._api_client_ctx.reset(token)

    assert seen == {"query": "logo", "limit": 5, "offset": 20}


def test_search_actions_tool_declares_offset():
    from src.autobots.mcp_server.tools import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "autobots_search_actions")
    assert "offset" in tool["inputSchema"]["properties"], "offset must be reachable by callers"
