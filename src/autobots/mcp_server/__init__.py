"""Autobots MCP Server — hosted Streamable HTTP endpoint.

Mounts into the main FastAPI app at /v1/mcp/.
Exposes the MCP tools for running actions, action graphs, action teams, and tools.

Usage from main.py:
    _mcp_server = create_mcp_server()
    app.mount("/v1/mcp", _mcp_server)
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import FunctionTool
from loguru import logger
from starlette.types import ASGIApp

from src.autobots.core.settings import SettingsProvider
from src.autobots.mcp_server.handlers import dispatch
from src.autobots.mcp_server.middleware import MCPAuthMiddleware
from src.autobots.mcp_server.tools import TOOLS

# ---------------------------------------------------------------------------
# Tool factory — wraps dispatch() for each tool
# ---------------------------------------------------------------------------


def _make_tool_fn(tool_name: str):
    """Create an async callable that dispatches to the handler for `tool_name`."""

    async def _fn(**kwargs: Any) -> str:
        return await dispatch(tool_name, kwargs)

    _fn.__name__ = tool_name
    return _fn


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------


def create_mcp_server() -> ASGIApp:
    """Build the MCP ASGI app (wrapped with auth middleware).

    Must be called AFTER SettingsProvider.set().  Reads SUPABASE_URL and
    SUPABASE_ANON_KEY from settings.

    Returns an ASGI app ready for `app.mount("/v1/mcp", ...)`.
    """
    mcp = FastMCP(
        name="autobots",
        instructions=(
            "Autobots is an AI orchestration platform. Use the tools to run actions, "
            "action graphs, action teams, and built-in tools. First use list/search tools "
            "to discover what's available, then run what you need. "
            'Authenticate once by POSTing {"email", "password"} to /v1/mcp/auth, then send '
            "the returned token as an Authorization: Bearer header on every request. When a "
            "response is 401 with X-Autobots-Auth-Error: token_invalid, repeat the exchange."
        ),
    )

    # Register every tool in TOOLS.
    # FunctionTool is constructed directly (rather than via mcp.tool()) so the
    # hand-written JSON Schemas in TOOLS are used verbatim as `parameters`,
    # instead of being inferred from the **kwargs signature of _make_tool_fn.
    for tool_def in TOOLS:
        name = tool_def["name"]
        mcp.add_tool(
            FunctionTool(
                name=name,
                description=tool_def["description"],
                parameters=tool_def["inputSchema"],
                output_schema=None,
                fn=_make_tool_fn(name),
            )
        )

    logger.info(
        "MCP server created with {} tools; API base URL: {!r}",
        len(TOOLS),
        SettingsProvider.sget().APP_BASE_URL,
    )

    # Build Streamable HTTP ASGI app.
    # stateless_http=True is required: the main app runs with multiple uvicorn
    # workers, and stateful mode keeps the per-session transport in one worker's
    # memory. A client's Mcp-Session-Id minted by one worker is unknown to the
    # others, so follow-up requests fail. Stateless mode builds a fresh transport
    # per request, so any worker can serve any request. The tools are all plain
    # request/response, so nothing here needs a long-lived session.
    mcp_asgi = mcp.http_app(path="/", stateless_http=True)

    # Wrap with auth middleware
    return MCPAuthMiddleware(mcp_asgi)
