import json

from src.autobots.action_run_trace import (
    build_openrouter_request,
    format_trace_for_llm,
    join_action_run,
    load_skill,
)
from src.autobots.action_run_trace_tools import apply_tool_wrappers

_MARKER = "Output stored in file url: "
_MODEL = "meta-llama/llama-3.1-8b-instruct"


def _document(stored: str) -> dict:
    return {
        "id": "run-tools",
        "name": "main agent",
        "status": "success",
        "error_message": None,
        "result": {
            "id": "action-1",
            "type": "text2text_llm_chat_openai",
            "config": {
                "provider": "openai",
                "model": "gpt-4.1",
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "run_terminal_command", "description": "Run a command"},
                    }
                ],
                "messages": [
                    {"role": "system", "content": "You are the main agent."},
                    {"role": "user", "content": "करो"},
                    {
                        "role": "assistant",
                        "content": "शुरू",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "run_terminal_command",
                                    "arguments": "{\"command\":\"cat\"}",
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": stored},
                    {"role": "assistant", "content": "हो गया"},
                ],
            },
            "results": [
                {
                    "input": {"text": "करो", "file_ids": ["file-1"]},
                    "output": {"blocks": [{"text": "हो गया", "urls": ["https://example.com/out.csv"]}]},
                }
            ],
        },
    }


def test_openrouter_user_message_is_stage_1_5_and_not_stage_1():
    stored = f"a,b\n1,2\n...\n\n{_MARKER}https://storage.example/files/user-1/out.txt"
    document = _document(stored)
    stage1 = join_action_run(document)
    stage15 = apply_tool_wrappers(format_trace_for_llm(stage1))
    request = build_openrouter_request(document, model=_MODEL)

    assert request["model"] == _MODEL
    assert [message["role"] for message in request["messages"]] == ["system", "user"]
    assert request["messages"][0]["content"] == load_skill()
    payload = json.loads(request["messages"][1]["content"])
    assert payload == stage15
    assert payload != stage1
    assert list(payload) == ["context", "events"]
    assert payload["events"][1]["turn"] == 1
    assert payload["events"][1]["kind"] == "user_input"
    body = request["messages"][1]["content"]
    assert json.dumps(stage1, ensure_ascii=False) not in body
    assert json.dumps(stage1, ensure_ascii=False) not in json.dumps(request, ensure_ascii=False)
    assert _MARKER not in body
    assert "external_references" not in body
    assert "tool_links" not in body
    assert '"execution"' not in body
    assert "करो" in body
    assert "file-1" in body


def test_openrouter_request_keeps_skill_override_and_model_error():
    document = {"id": "run-1", "status": "success", "result": {"config": {"messages": []}, "results": []}}
    request = build_openrouter_request(document, model=_MODEL, skill="CUSTOM SKILL")

    assert request["messages"][0]["content"] == "CUSTOM SKILL"
    try:
        build_openrouter_request(document, model="")
    except ValueError as exc:
        assert str(exc) == "model is required"
    else:
        raise AssertionError("empty model must be rejected")


def test_openrouter_payload_uses_retrieved_tool_text():
    url = "https://storage.example/files/user-1/out.txt"
    stored = f"a,b\n1,2\n...\n\n{_MARKER}{url}"
    full = "a,b\n1,2\n3,4"
    document = _document(stored)
    request = build_openrouter_request(
        document,
        model=_MODEL,
        retrieve_external=lambda file_url: full if file_url == url else None,
    )

    payload = json.loads(request["messages"][1]["content"])
    stage1 = join_action_run(document, retrieve_external=lambda file_url: full if file_url == url else None)
    assert payload == apply_tool_wrappers(format_trace_for_llm(stage1))
    call = next(event for event in payload["events"] if event["kind"] == "tool_call")
    tool = next(event for event in payload["events"] if event["kind"] == "tool_result")
    assert call["effect"]["command"] == "cat"
    assert call["effect"]["body_omitted"] is True
    assert "arguments" not in call
    assert tool["body_omitted"] is True
    assert "content" not in tool
    assert full not in request["messages"][1]["content"]
    assert _MARKER not in request["messages"][1]["content"]
    assert stored not in request["messages"][1]["content"]
