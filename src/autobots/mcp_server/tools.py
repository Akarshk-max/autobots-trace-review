"""MCP tool definitions — name, description, and JSON Schema for each tool."""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    # -- Run tools -----------------------------------------------------------
    {
        "name": "autobots_run_action",
        "description": (
            "Run an Autobots action by its ID. Actions are pre-configured AI agents "
            "that can perform text generation, image generation, audio transcription, "
            "browser automation, API calls, and more. Returns an action_result_id "
            "that can be used with autobots_get_result to check status and retrieve output."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action_id": {
                    "type": "string",
                    "description": "The ID of the action to run (e.g. 'act_abc123').",
                },
                "input": {
                    "type": "object",
                    "description": (
                        "The input payload for the action. Shape depends on the action type. "
                        "Common fields: 'text' (string), 'urls' (list of URLs), 'file_ids' (list)."
                    ),
                },
            },
            "required": ["action_id", "input"],
        },
    },
    {
        "name": "autobots_run_tool",
        "description": (
            "Run a built-in Autobots tool by name. Tools are lower-level capabilities like "
            "file reading, terminal commands, web search, code execution, etc. "
            "Returns a tool_run_id that can be used with autobots_get_result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "The name of the tool to run (e.g. 'run_terminal_command', 'read_web_url').",
                },
                "tool_args": {
                    "type": "object",
                    "description": "The arguments for the tool. Shape depends on the tool.",
                },
            },
            "required": ["tool_name", "tool_args"],
        },
    },
    {
        "name": "autobots_run_action_graph",
        "description": (
            "Run an Autobots action graph (DAG workflow) by its ID. Action graphs chain "
            "multiple actions together with conditional logic, loops, and parallel execution. "
            "Returns an action_graph_result_id for use with autobots_get_result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action_graph_id": {
                    "type": "string",
                    "description": "The ID of the action graph to run.",
                },
                "input": {
                    "type": "object",
                    "description": "The input payload for the action graph.",
                },
            },
            "required": ["action_graph_id", "input"],
        },
    },
    {
        "name": "autobots_run_action_team",
        "description": (
            "Run an Autobots action team by its ID. Action teams coordinate multiple AI agents "
            "(leader + members) across multiple iterations to complete complex tasks. "
            "Returns an action_team_result_id for use with autobots_get_result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action_team_id": {
                    "type": "string",
                    "description": "The ID of the action team to run.",
                },
                "input": {
                    "type": "object",
                    "description": "The input payload for the action team.",
                },
            },
            "required": ["action_team_id", "input"],
        },
    },
    # -- Result tools ---------------------------------------------------------
    {
        "name": "autobots_get_result",
        "description": (
            "Get the status and result of any Autobots run. Works with action runs, "
            "tool runs, action graph runs, and action team runs. The run_id can be "
            "any of: action_result_id, tool_run_id, action_graph_result_id, or "
            "action_team_result_id. Returns the current status and output if complete."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": (
                        "The run ID to check. Can be an action_result_id, tool_run_id, "
                        "action_graph_result_id, or action_team_result_id."
                    ),
                },
                "run_type": {
                    "type": "string",
                    "enum": ["action", "tool", "action_graph", "action_team"],
                    "description": (
                        "The type of run. 'action' for action runs, 'tool' for tool runs, "
                        "'action_graph' for graph runs, 'action_team' for team runs."
                    ),
                },
            },
            "required": ["run_id", "run_type"],
        },
    },
    # -- List / Discovery tools -----------------------------------------------
    {
        "name": "autobots_list_actions",
        "description": (
            "List available Autobots actions. Returns actions owned by or shared "
            "with the user. Does NOT include system actions — use "
            "autobots_list_system_actions for those. Can filter by name, type, "
            "and published status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Optional filter by action name (partial match).",
                },
                "action_type": {
                    "type": "string",
                    "description": "Optional filter by action type (e.g. 'text2text_llm_chat_openai').",
                },
                "is_published": {
                    "type": "boolean",
                    "description": "If true, only return published actions.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 100).",
                    "default": 100,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset (default 0).",
                    "default": 0,
                },
            },
        },
    },
    {
        "name": "autobots_list_system_actions",
        "description": (
            "List Autobots system actions — the platform's built-in agents "
            "(e.g. Autocode coding agents, AutoBots, AutoSpark) that are available "
            "to every user. These are separate from the user's own actions returned "
            "by autobots_list_actions. Run one with autobots_run_action."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Optional filter by action name (partial match).",
                },
                "action_type": {
                    "type": "string",
                    "description": "Optional filter by action type (e.g. 'openai', 'claude').",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 100).",
                    "default": 100,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset (default 0).",
                    "default": 0,
                },
            },
        },
    },
    {
        "name": "autobots_list_tools",
        "description": (
            "List all available built-in Autobots tools. Returns tool names like "
            "'run_terminal_command', 'read_web_url', 'file_system_tool', etc."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "autobots_list_action_graphs",
        "description": (
            "List available Autobots action graphs (DAG workflows). Can filter by name "
            "and published status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Optional filter by graph name.",
                },
                "is_published": {
                    "type": "boolean",
                    "description": "If true, only return published graphs.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 100).",
                    "default": 100,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset (default 0).",
                    "default": 0,
                },
            },
        },
    },
    {
        "name": "autobots_list_action_teams",
        "description": (
            "List available Autobots action teams (multi-agent teams). Can filter by name "
            "and published status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Optional filter by team name.",
                },
                "is_published": {
                    "type": "boolean",
                    "description": "If true, only return published teams.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 100).",
                    "default": 100,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset (default 0).",
                    "default": 0,
                },
            },
        },
    },
    {
        "name": "autobots_search_actions",
        "description": (
            "Search for Autobots actions by text query. Searches across action names, "
            "descriptions, and types. Returns matching actions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query string.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10).",
                    "default": 10,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset (default 0).",
                    "default": 0,
                },
            },
            "required": ["query"],
        },
    },
    # -- Control tools --------------------------------------------------------
    {
        "name": "autobots_cancel_run",
        "description": (
            "Cancel a running action. Uses the action_result_id returned by "
            "autobots_run_action. Only action runs can be cancelled — there is no "
            "cancel for tool, action graph, or action team runs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action_result_id": {
                    "type": "string",
                    "description": "The action_result_id of the run to cancel.",
                },
            },
            "required": ["action_result_id"],
        },
    },
]
