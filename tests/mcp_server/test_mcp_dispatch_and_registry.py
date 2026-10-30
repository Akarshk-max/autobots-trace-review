"""Dispatch error-signalling and tool-registry consistency."""

import pytest
from fastmcp.exceptions import ToolError

from src.autobots.mcp_server import middleware
from src.autobots.mcp_server.client import APIClientError
from src.autobots.mcp_server.handlers import HANDLERS, dispatch
from src.autobots.mcp_server.tools import TOOLS


class _FakeClient:
    """Stands in for APIClient; each method raises or returns as configured."""

    def __init__(self, exc=None, result=None):
        self._exc = exc
        self._result = result if result is not None else {"ok": True}

    def __getattr__(self, _name):
        async def _call(*a, **kw):
            if self._exc:
                raise self._exc
            return self._result

        return _call


@pytest.fixture
def fake_client():
    token = middleware._api_client_ctx.set(_FakeClient())
    yield
    middleware._api_client_ctx.reset(token)


# --- error signalling -------------------------------------------------------
# A failed tool call must raise so MCP marks the response isError. Returning the
# message as an ordinary string reports a *successful* call whose content merely
# reads like an error, which callers cannot distinguish from a real result.


@pytest.mark.asyncio
async def test_unknown_tool_raises(fake_client):
    with pytest.raises(ToolError, match="Unknown tool"):
        await dispatch("autobots_not_a_tool", {})


@pytest.mark.asyncio
async def test_api_error_raises_instead_of_returning_a_string():
    token = middleware._api_client_ctx.set(
        _FakeClient(exc=APIClientError(404, "Action result not found"))
    )
    try:
        with pytest.raises(ToolError, match="404"):
            await dispatch("autobots_get_result", {"run_id": "x", "run_type": "action"})
    finally:
        middleware._api_client_ctx.reset(token)


@pytest.mark.asyncio
async def test_unknown_run_type_raises(fake_client):
    with pytest.raises(ToolError, match="run_type"):
        await dispatch("autobots_get_result", {"run_id": "x", "run_type": "nonsense"})


@pytest.mark.asyncio
async def test_missing_required_argument_raises(fake_client):
    with pytest.raises(ToolError, match="Missing required argument"):
        await dispatch("autobots_run_action", {})


@pytest.mark.asyncio
async def test_successful_call_returns_formatted_json(fake_client):
    out = await dispatch("autobots_list_tools", {})
    assert '"ok": true' in out


# --- registry consistency ---------------------------------------------------


def test_every_declared_tool_has_a_handler():
    declared = {t["name"] for t in TOOLS}
    missing = declared - set(HANDLERS)
    assert not missing, f"tools declared with no handler: {sorted(missing)}"


def test_every_handler_is_declared_as_a_tool():
    declared = {t["name"] for t in TOOLS}
    orphaned = set(HANDLERS) - declared
    assert not orphaned, f"handlers not exposed as tools: {sorted(orphaned)}"


def test_tool_names_are_unique():
    names = [t["name"] for t in TOOLS]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t["name"])
def test_tool_schema_is_well_formed(tool):
    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    assert isinstance(schema.get("properties", {}), dict)
    assert tool["description"].strip()
    unknown = set(schema.get("required", [])) - set(schema.get("properties", {}))
    assert not unknown, f"required names not in properties: {sorted(unknown)}"
