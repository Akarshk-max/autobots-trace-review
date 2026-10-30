import json

import pytest

from src.autobots.conn.claude.claude_chat import AnthropicChat
from src.autobots.conn.gemini.gemini_chat import GeminiChatRuntime
from src.autobots.conn.openai.openai_responses.tool_args_streamer import ToolArgsStreamer
from src.autobots.data_model.context import Context

TOOL_ARGS = {"command": "ls -la", "timeout": 120}


class RecordingNotify:
    topic = "run-id"

    def __init__(self):
        self.messages: list[str] = []

    def __call__(self, ctx, message):
        self.messages.append(message)


def _is_tool_message(message: str) -> bool:
    """Mirrors how the CLI and web clients recognise a tool message."""
    return message.startswith("Calling tool:") and "\nArguments:" in message


def test_claude_renders_tool_arguments_in_the_shared_format():
    notify = RecordingNotify()
    streamer = ToolArgsStreamer("bash", key_char_limit=100, value_char_limit=100)
    streamer.ingest(json.dumps(TOOL_ARGS))

    AnthropicChat._notify_tool_args(ctx=Context(), notify=notify, streamer=streamer)

    assert len(notify.messages) == 1
    assert _is_tool_message(notify.messages[0])
    assert "- command: ls -la" in notify.messages[0]
    assert "- timeout: 120" in notify.messages[0]


def test_claude_does_not_resend_unchanged_tool_arguments():
    notify = RecordingNotify()
    streamer = ToolArgsStreamer("bash", key_char_limit=100, value_char_limit=100)
    streamer.ingest(json.dumps(TOOL_ARGS))

    AnthropicChat._notify_tool_args(ctx=Context(), notify=notify, streamer=streamer)
    AnthropicChat._notify_tool_args(ctx=Context(), notify=notify, streamer=streamer)

    assert len(notify.messages) == 1


def test_gemini_renders_tool_arguments_in_the_shared_format():
    notify = RecordingNotify()

    GeminiChatRuntime._notify_tool_args(ctx=Context(), notify=notify, name="bash", args=TOOL_ARGS)

    assert len(notify.messages) == 1
    assert _is_tool_message(notify.messages[0])
    assert "- command: ls -la" in notify.messages[0]


@pytest.mark.parametrize("args", [None, {}])
def test_gemini_renders_a_tool_call_that_has_no_arguments(args):
    notify = RecordingNotify()

    GeminiChatRuntime._notify_tool_args(ctx=Context(), notify=notify, name="ping", args=args)

    assert notify.messages == ["Calling tool: ping\nArguments:\n- (streaming arguments...)"]


def test_claude_tool_arguments_stream_incrementally():
    """Partial JSON must render usable fields before the tool call completes."""
    notify = RecordingNotify()
    streamer = ToolArgsStreamer("bash", key_char_limit=100, value_char_limit=100)
    full = json.dumps(TOOL_ARGS)

    for start in range(0, len(full), 5):
        streamer.ingest(full[start : start + 5])
        AnthropicChat._notify_tool_args(ctx=Context(), notify=notify, streamer=streamer)

    assert len(notify.messages) > 1
    assert all(_is_tool_message(message) for message in notify.messages)
    assert "- command: ls -la" in notify.messages[-1]
