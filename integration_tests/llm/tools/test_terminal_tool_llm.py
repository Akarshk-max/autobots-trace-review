from src.autobots.data_model.app_state import AppState

"""
Tests that an LLM (GPT-5.2) can correctly use run_terminal_command with
session_id for persistent shell sessions.

Run:
    poetry run pytest tests/llm/tools/test_terminal_tool_llm.py -v -s --timeout=300
"""

import json

import pytest
from openai.types.responses import ResponseInputTextParam
from openai.types.responses.response_input_param import Message

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.openai.openai_responses.openai_responses import (
    LlmResponsesOpenaiConfig,
)

TERMINAL_TOOL_DESCRIPTION = """\
You have access to the user's local machine terminal via the run_terminal_command tool.

TWO MODES:
1. One-off (session_id=null): Run a single independent command. No state carries over.
2. Persistent session (session_id="new" or existing ID): Shell state (cd, env vars, aliases) \
persists across calls. Use this when the user needs to work in a specific directory, set up \
environment, or run a sequence of related commands.

WHEN TO USE PERSISTENT SESSIONS:
- User says "go to /some/dir and run X" → session_id="new", then reuse the returned session_id
- User needs cd, export, or source to persist → use session_id
- Multi-step workflows (build, test, deploy) → use session_id
- Simple one-off commands (ls, cat, whoami) → omit session_id

EXAMPLES:
- One-off: {"command": "whoami", "session_id": null}
- New session: {"command": "cd /tmp && pwd", "session_id": "new"}
- Reuse session: {"command": "ls", "session_id": "<returned-id>"}
"""


def _extract_terminal_tool_calls(input_items: list[dict]) -> list[dict]:
    """Extract run_terminal_command function call arguments from Responses API input."""
    tool_calls = []
    for item in input_items:
        if item.get("type") == "function_call" and item.get("name") == "run_terminal_command":
            args = json.loads(item.get("arguments", "{}"))
            tool_calls.append(args)
    return tool_calls


@pytest.mark.asyncio
async def test_llm_uses_session_id_for_persistent_commands(
    ctx, db, user, set_test_settings
):
    """LLM should use session_id='new' when asked to cd and run commands."""
    action_config = LlmResponsesOpenaiConfig(
        provider="openai",
        input=[
            Message(
                content=[
                    ResponseInputTextParam(
                        text=TERMINAL_TOOL_DESCRIPTION,
                        type="input_text",
                    )
                ],
                role="system",
            ),
            Message(
                content=[
                    ResponseInputTextParam(
                        text=(
                            "Go to /tmp directory, create a file called test_session.txt "
                            "with the content 'hello from session', then list the files "
                            "in the current directory to confirm it was created. "
                            "Make sure the cd persists between commands."
                        ),
                        type="input_text",
                    )
                ],
                role="user",
            ),
        ],
        model="gpt-5.2",
        tools=["run_terminal_command"],
        store=True,
        truncation="auto",
    )

    action_doc = ActionDoc(
        id="test_llm_session",
        user_id=user.id,
        name="test_llm_terminal_session",
        type=ActionType.text2text_llm_responses_openai,
        config=action_config.model_dump(exclude_none=True),
    )

    action_input = DataBlock(text="")
    action_run_obj = await ActionFactory.run_action(
        ctx, AppState(), action_doc, action_input.model_dump(), user, db=db
    )

    assert action_run_obj.output_dict
    print(f"\n[llm_session] output: {action_run_obj.output_dict}")

    # Check that the LLM used session_id in its tool calls
    input_items = action_run_obj.config_dict.get("input", [])
    all_tool_calls = _extract_terminal_tool_calls(input_items)
    tool_calls_with_session = [tc for tc in all_tool_calls if tc.get("session_id") is not None]

    print(f"\n[llm_session] all tool calls: {all_tool_calls}")
    print(f"[llm_session] tool calls with session_id: {tool_calls_with_session}")
    assert len(tool_calls_with_session) > 0, (
        "LLM did not use session_id parameter for persistent shell commands"
    )


@pytest.mark.asyncio
async def test_llm_uses_oneoff_for_simple_command(
    ctx, db, user, set_test_settings
):
    """LLM should use session_id=null for a simple one-off command."""
    action_config = LlmResponsesOpenaiConfig(
        provider="openai",
        input=[
            Message(
                content=[
                    ResponseInputTextParam(
                        text=TERMINAL_TOOL_DESCRIPTION,
                        type="input_text",
                    )
                ],
                role="system",
            ),
            Message(
                content=[
                    ResponseInputTextParam(
                        text="What is the current date? Use the terminal to find out.",
                        type="input_text",
                    )
                ],
                role="user",
            ),
        ],
        model="gpt-5.2",
        tools=["run_terminal_command"],
        store=True,
        truncation="auto",
    )

    action_doc = ActionDoc(
        id="test_llm_oneoff",
        user_id=user.id,
        name="test_llm_terminal_oneoff",
        type=ActionType.text2text_llm_responses_openai,
        config=action_config.model_dump(exclude_none=True),
    )

    action_input = DataBlock(text="")
    action_run_obj = await ActionFactory.run_action(
        ctx, AppState(), action_doc, action_input.model_dump(), user, db=db
    )

    assert action_run_obj.output_dict
    print(f"\n[llm_oneoff] output: {action_run_obj.output_dict}")

    # Check that the LLM used run_terminal_command without session_id
    input_items = action_run_obj.config_dict.get("input", [])
    all_tool_calls = _extract_terminal_tool_calls(input_items)

    print(f"\n[llm_oneoff] tool calls: {all_tool_calls}")
    assert len(all_tool_calls) > 0, "LLM did not call run_terminal_command"
    # For a simple date command, session_id should be null/absent
    assert all_tool_calls[0].get("session_id") is None, (
        "LLM used session_id for a simple one-off command"
    )
