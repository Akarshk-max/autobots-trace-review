"""Centralized default tool lists for LLM chat actions."""

from typing import Tuple


def _dedup(*lists: Tuple[str, ...]) -> Tuple[str, ...]:
    """Order-preserving dedup across multiple tuples."""
    return tuple(dict.fromkeys(name for lst in lists for name in lst))


# External tools common to every LLM action that supports tool calling.
COMMON_EXTERNAL_TOOLS: Tuple[str, ...] = (
    "get_date_time",
    "read_web_url",
    "google_search_main",
    "get_database_connection_names",
    "get_mcp_server_names",
    "enable_mcp",
    "get_connected_apps",
    "save_files_tool",
    "api_call",
)

# OpenAI Chat Completions (action_text2text_llm_chat_openai_v2.py)
OPENAI_CHAT_DEFAULT_TOOLS: Tuple[str, ...] = _dedup(
    COMMON_EXTERNAL_TOOLS,
    (
        "file_search_tool",
        "coding_assistant",
    ),
)

# OpenAI Responses v1 (conn/openai/openai_responses/openai_responses.py)
OPENAI_RESPONSES_DEFAULT_TOOLS: Tuple[str, ...] = _dedup(
    COMMON_EXTERNAL_TOOLS,
    (
        "code_interpreter",
        "file_search",
    ),
)

# OpenAI Responses v2 (conn/openai/openai_responses/openai_responses_v2.py)
OPENAI_RESPONSES_V2_DEFAULT_TOOLS: Tuple[str, ...] = _dedup(
    COMMON_EXTERNAL_TOOLS,
    (
        "code_interpreter",
        "file_search",
        "file_uploader_openai",
        "chat_history_query_openai",
    ),
)

# Claude (action_text2text_llm_chat_claude.py) - injected at runtime.
# Claude Vertex inherits from Claude, no separate list.
CLAUDE_DEFAULT_TOOLS: Tuple[str, ...] = _dedup(
    COMMON_EXTERNAL_TOOLS,
    (
        "tool_results_query_claude",
        "chat_history_query_claude",
        "claude_container_file_attach",
        # Enable Claude code execution by default.
        "code_interpreter",
    ),
)

# Gemini (action_text2text_llm_chat_gemini.py) - injected at runtime.
# Gemini Vertex inherits from Gemini, no separate list.
GEMINI_DEFAULT_TOOLS: Tuple[str, ...] = _dedup(
    COMMON_EXTERNAL_TOOLS,
    (
        "gemini_context_tool",
        "gemini_code_execution_tool",
        "gemini_chat_memory_tool",
    ),
)

GROK_DEFAULT_TOOLS: Tuple[str, ...] = _dedup(
    COMMON_EXTERNAL_TOOLS,
    (
        "grok_chat_history_query",
    ),
)

# OpenRouter (action_text2text_llm_chat_openrouter.py)
OPENROUTER_DEFAULT_TOOLS: Tuple[str, ...] = _dedup(
    COMMON_EXTERNAL_TOOLS,
    (
        "openai_vector_store_tool",
        "daytona_sandbox",
        "openrouter_inspect",
    ),
)
