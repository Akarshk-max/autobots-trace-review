import copy
import json

from src.autobots.action_run_trace import build_openrouter_request, format_trace_for_llm, join_action_run
from src.autobots.action_run_trace_tools import apply_tool_wrappers, project_tool_call


def test_file_edit_keeps_the_operation_and_drops_the_line_text():
    arguments = json.dumps(
        {
            "operation": "replace_in_file",
            "payload": {
                "path": "src/app.py",
                "old_text": "DEBUG = True",
                "new_text": "DEBUG = False",
            },
        }
    )
    result = json.dumps({"success": True, "data": {"content": "ENTIRE FILE\nDEBUG = False\n"}})

    effect = project_tool_call("file_system_tool", arguments, result)

    assert effect["operation"] == "replace_in_file"
    assert effect["path"] == "src/app.py"
    assert effect["success"] is True
    assert effect["changed"] is True
    assert effect["body_omitted"] is True
    assert "DEBUG = True" not in json.dumps(effect)
    assert "ENTIRE FILE" not in json.dumps(effect)


def test_file_read_does_not_claim_a_change():
    arguments = json.dumps({"operation": "read_file", "payload": {"path": "notes.txt", "mode": "full"}})
    result = json.dumps({"success": True, "data": {"content": "line one\nline two"}})

    effect = project_tool_call("file_system_tool", arguments, result)

    assert effect["operation"] == "read_file"
    assert effect["path"] == "notes.txt"
    assert effect["success"] is True
    assert "changed" not in effect
    assert "line one" not in json.dumps(effect)


def test_terminal_keeps_the_command_and_drops_stdout():
    effect = project_tool_call(
        "run_terminal_command",
        json.dumps({"command": "cat notes.txt"}),
        "a,b\n1,2\n3,4\nOutput stored in file url: https://example.com/out.txt",
    )

    assert effect == {
        "tool": "run_terminal_command",
        "body_omitted": True,
        "command": "cat notes.txt",
    }


def test_search_keeps_the_query_and_drops_the_page():
    effect = project_tool_call(
        "read_web_url",
        json.dumps({"url": "https://example.com/docs"}),
        "This page has a very long article body that must stay out of the trace.",
    )

    assert effect["tool"] == "read_web_url"
    assert effect["url"] == "https://example.com/docs"
    assert effect["body_omitted"] is True
    assert "article body" not in json.dumps(effect)


def test_unknown_tool_keeps_a_short_id_and_drops_the_body():
    effect = project_tool_call(
        "get_date_time",
        json.dumps({"id": "clock-1", "content": "x" * 500}),
        "Saturday morning in a long locale string " * 20,
    )

    assert effect["tool"] == "get_date_time"
    assert effect["id"] == "clock-1"
    assert effect["body_omitted"] is True
    assert "content" not in effect
    assert "Saturday" not in json.dumps(effect)


def test_apply_tool_wrappers_projects_events_without_changing_stage_1_5():
    formatted = {
        "context": {"run_id": "run-1"},
        "events": [
            {
                "index": 0,
                "kind": "tool_call",
                "name": "file_system_tool",
                "call_id": "call_1",
                "arguments": json.dumps(
                    {
                        "operation": "write_file",
                        "payload": {"path": "out.txt", "content": "FULL WRITE"},
                    }
                ),
                "result_message_index": 1,
            },
            {
                "index": 1,
                "kind": "tool_result",
                "call_id": "call_1",
                "message_index": 1,
                "content": json.dumps({"success": True, "data": {"content": "FULL WRITE"}}),
                "content_source": "message",
            },
        ],
    }
    before = copy.deepcopy(formatted)

    projected = apply_tool_wrappers(formatted)

    assert formatted == before
    call = projected["events"][0]
    result = projected["events"][1]
    assert call["effect"]["operation"] == "write_file"
    assert call["effect"]["path"] == "out.txt"
    assert call["effect"]["changed"] is True
    assert "arguments" not in call
    assert result["body_omitted"] is True
    assert "content" not in result
    assert "FULL WRITE" not in json.dumps(projected)


def test_openrouter_payload_uses_the_file_wrapper():
    document = {
        "id": "run-edit",
        "status": "success",
        "result": {
            "type": "text2text_llm_chat_openai",
            "config": {
                "tools": [{"type": "function", "function": {"name": "file_system_tool"}}],
                "messages": [
                    {"role": "user", "content": "बदलो"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "file_system_tool",
                                    "arguments": json.dumps(
                                        {
                                            "operation": "replace_in_file",
                                            "payload": {
                                                "path": "src/app.py",
                                                "old_text": "OLD LINE",
                                                "new_text": "NEW LINE",
                                            },
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_1",
                        "content": json.dumps(
                            {"success": True, "data": {"content": "the whole file with NEW LINE"}}
                        ),
                    },
                ],
            },
            "results": [
                {"input": {"text": "बदलो"}, "output": {"blocks": [{"text": "हो गया"}]}}
            ],
        },
    }

    request = build_openrouter_request(document, model="meta-llama/llama-3.1-8b-instruct")
    payload = json.loads(request["messages"][1]["content"])
    stage1 = join_action_run(document)

    assert payload == apply_tool_wrappers(format_trace_for_llm(stage1))
    body = request["messages"][1]["content"]
    assert "OLD LINE" not in body
    assert "NEW LINE" not in body
    assert "the whole file" not in body
    call = next(event for event in payload["events"] if event["kind"] == "tool_call")
    assert call["effect"]["path"] == "src/app.py"
    assert call["effect"]["operation"] == "replace_in_file"
    assert call["effect"]["changed"] is True
