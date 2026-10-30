"""MCP tool call handlers — dispatched by the FastMCP server.

Each handler calls internal /v1/ endpoints using the per-request APIClient
stored in the contextvar by MCPAuthMiddleware.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp.exceptions import ToolError
from loguru import logger

from src.autobots.mcp_server.client import APIClientError
from src.autobots.mcp_server.middleware import get_api_client


def _fmt(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str, ensure_ascii=False)


def _get_client():
    """Lazy client retrieval so handlers only touch the contextvar at call time."""
    return get_api_client()


async def handle_run_action(arguments: dict) -> str:
    client = _get_client()
    result = await client.run_action(arguments["action_id"], arguments["input"])
    return _fmt(result)


async def handle_run_tool(arguments: dict) -> str:
    client = _get_client()
    result = await client.run_tool(arguments["tool_name"], arguments["tool_args"])
    return _fmt(result)


async def handle_run_action_graph(arguments: dict) -> str:
    client = _get_client()
    result = await client.run_action_graph(arguments["action_graph_id"], arguments["input"])
    return _fmt(result)


async def handle_run_action_team(arguments: dict) -> str:
    client = _get_client()
    result = await client.run_action_team(arguments["action_team_id"], arguments["input"])
    return _fmt(result)


async def handle_get_result(arguments: dict) -> str:
    client = _get_client()
    run_type = arguments["run_type"]
    run_id = arguments["run_id"]
    if run_type == "action":
        result = await client.get_action_result(run_id)
    elif run_type == "tool":
        result = await client.get_tool_result(run_id)
    elif run_type == "action_graph":
        result = await client.get_action_graph_result(run_id)
    elif run_type == "action_team":
        result = await client.get_action_team_result(run_id)
    else:
        raise ToolError(f"Unknown run_type '{run_type}'")
    return _fmt(result)


async def handle_list_actions(arguments: dict) -> str:
    client = _get_client()
    result = await client.list_actions(
        name=arguments.get("name"),
        action_type=arguments.get("action_type"),
        is_published=arguments.get("is_published"),
        limit=arguments.get("limit", 100),
        offset=arguments.get("offset", 0),
    )
    return _fmt(result)


async def handle_list_system_actions(arguments: dict) -> str:
    client = _get_client()
    result = await client.list_system_actions(
        name=arguments.get("name"),
        action_type=arguments.get("action_type"),
        limit=arguments.get("limit", 100),
        offset=arguments.get("offset", 0),
    )
    return _fmt(result)


async def handle_list_tools(arguments: dict) -> str:
    client = _get_client()
    result = await client.list_tools()
    return _fmt(result)


async def handle_list_action_graphs(arguments: dict) -> str:
    client = _get_client()
    result = await client.list_action_graphs(
        name=arguments.get("name"),
        is_published=arguments.get("is_published"),
        limit=arguments.get("limit", 100),
        offset=arguments.get("offset", 0),
    )
    return _fmt(result)


async def handle_list_action_teams(arguments: dict) -> str:
    client = _get_client()
    result = await client.list_action_teams(
        name=arguments.get("name"),
        is_published=arguments.get("is_published"),
        limit=arguments.get("limit", 100),
        offset=arguments.get("offset", 0),
    )
    return _fmt(result)


async def handle_search_actions(arguments: dict) -> str:
    client = _get_client()
    result = await client.search_actions(
        query=arguments["query"],
        limit=arguments.get("limit", 10),
        offset=arguments.get("offset", 0),
    )
    return _fmt(result)


async def handle_cancel_run(arguments: dict) -> str:
    client = _get_client()
    result = await client.cancel_action_run(arguments["action_result_id"])
    return _fmt(result)


HANDLERS: dict[str, Any] = {
    "autobots_run_action": handle_run_action,
    "autobots_run_tool": handle_run_tool,
    "autobots_run_action_graph": handle_run_action_graph,
    "autobots_run_action_team": handle_run_action_team,
    "autobots_get_result": handle_get_result,
    "autobots_list_actions": handle_list_actions,
    "autobots_list_system_actions": handle_list_system_actions,
    "autobots_list_tools": handle_list_tools,
    "autobots_list_action_graphs": handle_list_action_graphs,
    "autobots_list_action_teams": handle_list_action_teams,
    "autobots_search_actions": handle_search_actions,
    "autobots_cancel_run": handle_cancel_run,
}


async def dispatch(tool_name: str, arguments: dict[str, Any]) -> str:
    """Route an MCP tool call to its handler.

    Failures are raised as ToolError so the MCP response is flagged isError.
    Returning the message as a normal string instead would report a *successful*
    tool call whose content happens to read like an error, which callers cannot
    distinguish from a real result.
    """
    handler = HANDLERS.get(tool_name)
    if handler is None:
        raise ToolError(f"Unknown tool: {tool_name}")
    try:
        return await handler(arguments)
    except ToolError:
        raise
    except APIClientError as e:
        logger.warning(f"Tool {tool_name} API error: {e}")
        raise ToolError(f"API error {e.status_code}: {e.detail}") from e
    except KeyError as e:
        logger.warning(f"Tool {tool_name} missing argument: {e}")
        raise ToolError(f"Missing required argument: {e}") from e
    except Exception as e:
        logger.exception(f"Tool {tool_name} unexpected error")
        raise ToolError(f"{type(e).__name__}: {e}") from e
