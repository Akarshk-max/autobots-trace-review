import copy

from src.autobots.action_run_trace import format_trace_for_llm, join_action_run

_MARKER = "Output stored in file url: "


def _count(value, needle: str) -> int:
    if isinstance(value, str):
        return value.count(needle)
    if isinstance(value, dict):
        return sum(_count(item, needle) for item in value.values())
    if isinstance(value, list):
        return sum(_count(item, needle) for item in value)
    return 0


def _view(document: dict, **kwargs):
    trace = join_action_run(document, **kwargs)
    return trace, format_trace_for_llm(trace)


def test_setup_does_not_consume_the_first_user_turn():
    trace, view = _view(
        {
            "id": "run-1",
            "_id": "run-1",
            "name": "main agent",
            "user_id": "user-1",
            "status": "success",
            "error_message": None,
            "created_at": "2026-01-02T03:04:05Z",
            "file_url": "https://storage.example/action_runs/user-1/run-1.json",
            "batch_id": "batch-1",
            "queued_inputs": {"inputs": [{"text": "later"}]},
            "custom_flag": {"kept": True},
            "result": {
                "id": "action-1",
                "type": "text2text_llm_chat_openai",
                "config": {
                    "provider": "openai",
                    "model": "gpt-4.1",
                    "temperature": 0.2,
                    "tools": [
                        {
                            "type": "function",
                            "function": {"name": "run_terminal_command", "description": "Run a command"},
                        }
                    ],
                    "messages": [
                        {"role": "system", "content": "You are the main agent."},
                        {"role": "user", "content": "पहला काम"},
                        {"role": "assistant", "content": "पहला जवाब"},
                    ],
                },
                "results": [
                    {"input": {"text": "पहला काम"}, "output": {"blocks": [{"text": "पहला जवाब"}]}}
                ],
            },
        }
    )

    context = view["context"]
    assert context["run_id"] == "run-1"
    assert context["name"] == "main agent"
    assert context["status"] == "success"
    assert context["error_message"] is None
    assert context["action_type"] == "text2text_llm_chat_openai"
    assert context["action_id"] == "action-1"
    assert context["model"] == "gpt-4.1"
    assert context["provider"] == "openai"
    assert context["available_tools"][0]["function"]["description"] == "Run a command"
    assert context["other_config"] == {"temperature": 0.2}
    assert context["other_run_fields"]["custom_flag"] == {"kept": True}
    assert context["batch_id"] == "batch-1"
    assert "_id" not in context["other_run_fields"]
    assert "messages" not in context["other_config"]
    setup, user, assistant = view["events"]
    assert setup["kind"] == "setup"
    assert "turn" not in setup
    assert setup["missing"] == ["result"]
    assert setup["content"] == "You are the main agent."
    assert user["kind"] == "user_input"
    assert user["turn"] == 1
    assert user["content_match"] == "same"
    assert "input_context" not in user
    assert assistant["kind"] == "assistant_response"
    assert assistant["turn"] == 1
    assert "output_context" not in assistant
    assert [event["index"] for event in view["events"]] == [0, 1, 2]
    assert not any(event["kind"] == "tool_call" for event in view["events"])
    assert trace["result"]["config"]["messages"][1]["content"] == "पहला काम"


def test_multiple_user_turns_start_at_one():
    _trace, view = _view(
        {
            "id": "run-turns",
            "status": "success",
            "result": {
                "config": {
                    "messages": [
                        {"role": "system", "content": "setup"},
                        {"role": "user", "content": "पहला"},
                        {"role": "assistant", "content": "जवाब एक"},
                        {"role": "user", "content": "दूसरा"},
                        {"role": "assistant", "content": "जवाब दो"},
                    ]
                },
                "results": [
                    {"input": {"text": "पहला"}, "output": {"blocks": [{"text": "जवाब एक"}]}},
                    {"input": {"text": "दूसरा"}, "output": {"blocks": [{"text": "जवाब दो"}]}},
                ],
            },
        }
    )

    assert [event.get("turn") for event in view["events"]] == [None, 1, 1, 2, 2]
    assert view["events"][3]["message_index"] == 3
    assert view["events"][3]["content"] == "दूसरा"
    assert _count(view, "पहला") == 1
    assert _count(view, "जवाब दो") == 1


def test_multiple_assistant_messages_in_one_turn():
    _trace, view = _view(
        {
            "id": "run-assistants",
            "status": "success",
            "result": {
                "config": {
                    "messages": [
                        {"role": "user", "content": "करो"},
                        {"role": "assistant", "content": "पहला"},
                        {"role": "assistant", "content": "दूसरा"},
                    ]
                },
                "results": [
                    {"input": {"text": "करो"}, "output": {"blocks": [{"text": "दूसरा"}]}}
                ],
            },
        }
    )

    assert [event["kind"] for event in view["events"]] == [
        "user_input",
        "assistant_message",
        "assistant_response",
    ]
    assert [event["turn"] for event in view["events"]] == [1, 1, 1]
    assert view["events"][1]["content"] == "पहला"
    assert view["events"][2]["content"] == "दूसरा"
    assert view["events"][1]["message_index"] == 1
    assert view["events"][2]["message_index"] == 2


def test_multiple_tool_calls_keep_source_order():
    _trace, view = _view(
        {
            "id": "run-calls",
            "status": "success",
            "result": {
                "config": {
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "run_terminal_command",
                                "description": "Run a command",
                                "parameters": {"type": "object"},
                            },
                        }
                    ],
                    "messages": [
                        {"role": "user", "content": "करो"},
                        {
                            "role": "assistant",
                            "content": "शुरू",
                            "tool_calls": [
                                {
                                    "id": "call_a",
                                    "type": "function",
                                    "function": {
                                        "name": "run_terminal_command",
                                        "arguments": "{\"command\":\"cat\"}",
                                        "vendor_flag": {"kept": True},
                                    },
                                },
                                {
                                    "id": "call_b",
                                    "type": "function",
                                    "function": {
                                        "name": "other_tool",
                                        "arguments": "{\"command\":\"ls\"}",
                                    },
                                },
                            ],
                        },
                        {"role": "tool", "tool_call_id": "call_a", "content": "file body"},
                        {"role": "tool", "tool_call_id": "call_b", "content": "list"},
                        {"role": "assistant", "content": "हो गया"},
                    ],
                },
                "results": [
                    {"input": {"text": "करो"}, "output": {"blocks": [{"text": "हो गया"}]}}
                ],
            },
        }
    )

    kinds = [event["kind"] for event in view["events"]]
    assert kinds == [
        "user_input",
        "assistant_message",
        "tool_call",
        "tool_call",
        "tool_result",
        "tool_result",
        "assistant_response",
    ]
    first, second = view["events"][2], view["events"][3]
    assert first["call_index"] == 0
    assert second["call_index"] == 1
    assert first["message_index"] == second["message_index"] == 1
    assert first["source"] == "result.config.messages[1].tool_calls[0]"
    assert first["arguments"] == "{\"command\":\"cat\"}"
    assert isinstance(first["arguments"], str)
    assert first["defined"] is True
    assert first["function_fields"] == {"vendor_flag": {"kept": True}}
    assert "description" not in first
    assert "parameters" not in first
    assert second["defined"] is False
    assert second["name"] == "other_tool"
    assert view["events"][4]["message_index"] == 2
    assert view["events"][5]["message_index"] == 3
    assert view["events"][4]["call_message_index"] == 1
    assert view["events"][4]["link_status"] == "paired"
    assert view["events"][6]["content"] == "हो गया"
    assert "output_context" not in view["events"][6]
    assert _count(view, "Run a command") == 1
    assert _count(view, "हो गया") == 1


def test_tool_results_stay_at_their_message_positions():
    _trace, view = _view(
        {
            "id": "run-order",
            "status": "success",
            "result": {
                "config": {
                    "messages": [
                        {"role": "user", "content": "करो"},
                        {
                            "role": "assistant",
                            "content": "पहले",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": "{}"},
                                }
                            ],
                        },
                        {"role": "tool", "tool_call_id": "call_1", "content": "रिजल्ट-1"},
                        {
                            "role": "assistant",
                            "content": "फिर",
                            "tool_calls": [
                                {
                                    "id": "call_2",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": "{}"},
                                }
                            ],
                        },
                        {"role": "tool", "tool_call_id": "call_2", "content": "रिजल्ट-2"},
                        {"role": "assistant", "content": "अंत"},
                    ]
                },
                "results": [{"input": {"text": "करो"}, "output": {"blocks": [{"text": "अंत"}]}}],
            },
        }
    )

    kinds = [event["kind"] for event in view["events"]]
    assert kinds == [
        "user_input",
        "assistant_message",
        "tool_call",
        "tool_result",
        "assistant_message",
        "tool_call",
        "tool_result",
        "assistant_response",
    ]
    first_result = view["events"][3]
    second_call = view["events"][5]
    assert first_result["content"] == "रिजल्ट-1"
    assert first_result["index"] < second_call["index"]
    assert first_result["message_index"] < second_call["message_index"]
    assert [event["turn"] for event in view["events"]] == [1, 1, 1, 1, 1, 1, 1, 1]


def test_matched_and_unmatched_tool_links_stay_visible():
    _trace, view = _view(
        {
            "id": "run-links",
            "status": "success",
            "result": {
                "config": {
                    "tools": [{"type": "function", "function": {"name": "read"}}],
                    "messages": [
                        {"role": "user", "content": "करो"},
                        {
                            "role": "assistant",
                            "content": "शुरू",
                            "tool_calls": [
                                {
                                    "id": "call_ok",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": "{}"},
                                },
                                {
                                    "id": "call_missing",
                                    "type": "function",
                                    "function": {"name": "missing_tool", "arguments": "{}"},
                                },
                            ],
                        },
                        {"role": "tool", "tool_call_id": "call_ok", "content": "body"},
                        {"role": "tool", "tool_call_id": "call_orphan", "content": "no call"},
                        {"role": "assistant", "content": "हो गया"},
                    ],
                },
                "results": [
                    {"input": {"text": "करो"}, "output": {"blocks": [{"text": "हो गया"}]}}
                ],
            },
        }
    )

    calls = [event for event in view["events"] if event["kind"] == "tool_call"]
    results = [event for event in view["events"] if event["kind"] == "tool_result"]
    assert calls[0]["link_status"] == "paired"
    assert calls[0]["defined"] is True
    assert "missing" not in calls[0]
    assert calls[0]["result_message_index"] == results[0]["message_index"]
    assert calls[1]["link_status"] == "unmatched_call"
    assert calls[1]["defined"] is False
    assert calls[1]["missing"] == ["tool_result"]
    assert "result_message_index" not in calls[1]
    assert results[1]["link_status"] == "unmatched_result"
    assert results[1]["call_id"] == "call_orphan"
    assert results[1]["missing"] == ["tool_call"]
    assert "call_message_index" not in results[1]
    assert results[0]["index"] < results[1]["index"] < view["events"][-1]["index"]


def test_duplicate_and_url_equivalent_input_text_is_not_repeated():
    _trace, exact = _view(
        {
            "id": "run-same",
            "status": "success",
            "result": {
                "config": {"messages": [{"role": "user", "content": "करो"}, {"role": "assistant", "content": "हो गया"}]},
                "results": [
                    {
                        "input": {"text": "करो", "urls": ["https://example.com/a.txt"]},
                        "output": {"blocks": [{"text": "हो गया"}]},
                    }
                ],
            },
        }
    )
    user = exact["events"][0]
    assert user["content"] == "करो"
    assert user["content_match"] == "same"
    assert "text" not in user["input_context"]
    assert user["input_context"]["text_omitted"] is True
    assert user["input_context"]["urls"] == ["https://example.com/a.txt"]
    assert _count(exact, "करो") == 1

    text = "पढ़ो"
    urls = ["https://example.com/b.txt"]
    stored = f"{text}\nUser-uploaded file URLs: {urls}"
    _trace, augmented = _view(
        {
            "id": "run-url",
            "status": "success",
            "result": {
                "config": {
                    "messages": [
                        {"role": "user", "content": stored},
                        {"role": "assistant", "content": "हो गया"},
                    ]
                },
                "results": [
                    {
                        "input": {"text": text, "urls": urls, "custom_note": {"kept": True}},
                        "output": {"blocks": [{"text": "हो गया"}]},
                    }
                ],
            },
        }
    )
    augmented_user = augmented["events"][0]
    assert augmented_user["content"] == stored
    assert augmented_user["content_match"] == "same"
    assert "text" not in augmented_user["input_context"]
    assert augmented_user["input_context"]["urls"] == urls
    assert augmented_user["input_context"]["custom_note"] == {"kept": True}
    assert _count(augmented, text) == 1


def test_differing_input_text_keeps_both_versions():
    image = {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
    payload = [{"type": "text", "text": "दूसरा"}, image]
    _trace, paired = _view(
        {
            "id": "run-differ-input",
            "status": "success",
            "result": {
                "config": {
                    "messages": [
                        {"role": "user", "content": payload},
                        {"role": "assistant", "content": "जवाब"},
                    ]
                },
                "results": [
                    {
                        "input": {
                            "text": "दूसरा",
                            "files": [{"filename": "a.txt", "content": "zzz"}],
                        },
                        "output": {"blocks": [{"text": "जवाब"}]},
                    }
                ],
            },
        }
    )

    user = paired["events"][0]
    assert user["turn_match"] == "paired"
    assert user["content"] == payload
    assert user["content_match"] == "differs"
    assert user["input_context"]["text"] == "दूसरा"
    assert user["input_context"]["files"] == [{"filename": "a.txt", "content": "zzz"}]
    assert "text_omitted" not in user["input_context"]

    _trace, separate = _view(
        {
            "id": "run-unpaired-text",
            "status": "success",
            "result": {
                "config": {
                    "messages": [
                        {"role": "user", "content": "एक"},
                        {"role": "assistant", "content": "जवाब"},
                    ]
                },
                "results": [
                    {"input": {"text": "दूसरा"}, "output": {"blocks": [{"text": "खाली"}]}}
                ],
            },
        }
    )
    assert separate["events"][0]["content"] == "एक"
    assert separate["events"][0]["content_match"] == "message_only"
    result = next(event for event in separate["events"] if event["content"] == "दूसरा")
    assert result["content_match"] == "result_only"
    assert result["turn_match"] == "unmatched_result"
    assert result["missing"] == ["messages"]


def test_duplicate_output_text_keeps_extra_block_fields():
    _trace, view = _view(
        {
            "id": "run-same-output",
            "status": "success",
            "result": {
                "config": {
                    "messages": [
                        {"role": "user", "content": "करो"},
                        {"role": "assistant", "content": "हो गया"},
                    ]
                },
                "results": [
                    {
                        "input": {"text": "करो"},
                        "output": {
                            "blocks": [
                                {
                                    "text": "हो गया",
                                    "urls": ["https://example.com/out.csv"],
                                    "file_ids": ["file-2"],
                                    "custom_note": {"kept": True},
                                }
                            ]
                        },
                    }
                ],
            },
        }
    )

    response = view["events"][1]
    block = response["output_context"]["blocks"][0]
    assert response["content"] == "हो गया"
    assert "text" not in block
    assert block["text_omitted"] is True
    assert block["text_match"] == "same_as_message"
    assert block["urls"] == ["https://example.com/out.csv"]
    assert block["file_ids"] == ["file-2"]
    assert block["custom_note"] == {"kept": True}
    assert _count(view, "हो गया") == 1


def test_differing_output_text_keeps_both_versions():
    _trace, view = _view(
        {
            "id": "run-differ-output",
            "status": "success",
            "result": {
                "config": {
                    "messages": [
                        {"role": "user", "content": "करो"},
                        {"role": "assistant", "content": "हो गया"},
                    ]
                },
                "results": [
                    {
                        "input": {"text": "करो"},
                        "output": {
                            "blocks": [
                                {"text": "अलग जवाब", "urls": ["https://example.com/out.csv"]}
                            ]
                        },
                    }
                ],
            },
        }
    )

    response = view["events"][1]
    block = response["output_context"]["blocks"][0]
    assert response["content"] == "हो गया"
    assert block["text"] == "अलग जवाब"
    assert block["text_match"] == "differs"
    assert block["urls"] == ["https://example.com/out.csv"]


def test_files_urls_metadata_and_unknown_fields_stay_on_the_input():
    _trace, view = _view(
        {
            "id": "run-files",
            "status": "success",
            "result": {
                "config": {
                    "messages": [
                        {"role": "user", "content": "देख"},
                        {"role": "assistant", "content": "देखा"},
                    ]
                },
                "results": [
                    {
                        "input": {
                            "text": "देख",
                            "urls": ["https://example.com/data.txt"],
                            "file_ids": ["file-1"],
                            "files": [{"filename": "data.txt", "content": "a,b"}],
                            "metadata": {"process_attachment_url": True},
                            "search_data": {"sources": [{"id": "s1"}]},
                            "mentions": ["ada@example.com"],
                            "source": "api",
                            "created_at": "2026-01-02T03:04:05Z",
                            "user_id": "user-1",
                            "custom_note": {"kept": True},
                        },
                        "output": {
                            "blocks": [
                                {
                                    "text": "देखा",
                                    "urls": ["https://example.com/out.csv"],
                                    "file_ids": ["file-2"],
                                    "files": [{"filename": "out.csv", "content": "a,b\n1,2"}],
                                }
                            ],
                            "custom_output": {"kept": True},
                        },
                    }
                ],
            },
        }
    )

    context = view["events"][0]["input_context"]
    assert context["files"] == [{"filename": "data.txt", "content": "a,b"}]
    assert context["file_ids"] == ["file-1"]
    assert context["urls"] == ["https://example.com/data.txt"]
    assert context["metadata"] == {"process_attachment_url": True}
    assert context["search_data"] == {"sources": [{"id": "s1"}]}
    assert context["mentions"] == ["ada@example.com"]
    assert context["source"] == "api"
    assert context["created_at"] == "2026-01-02T03:04:05Z"
    assert context["user_id"] == "user-1"
    assert context["custom_note"] == {"kept": True}
    assert context["text_omitted"] is True
    assert "text" not in context
    block = view["events"][1]["output_context"]["blocks"][0]
    assert block["file_ids"] == ["file-2"]
    assert block["files"] == [{"filename": "out.csv", "content": "a,b\n1,2"}]
    assert view["events"][1]["output_context"]["fields"] == {"custom_output": {"kept": True}}
    assert view["events"][0]["content"] == "देख"
    assert view["events"][1]["content"] == "देखा"
    assert "text" not in view["events"][1]["output_context"]["blocks"][0]


def test_image_and_multimodal_payloads_stay_structured():
    image = {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
    missing = {"type": "image_url", "image_url": {}}
    caption = {"type": "text", "text": "कैप्शन"}
    _trace, view = _view(
        {
            "id": "run-image",
            "status": "success",
            "result": {
                "config": {
                    "messages": [
                        {"role": "user", "content": "देख"},
                        {"role": "user", "content": [image, missing, caption]},
                        {"role": "assistant", "content": "देखा"},
                    ]
                },
                "results": [
                    {"input": {"text": "देख"}, "output": {"blocks": [{"text": "देखा"}]}}
                ],
            },
        }
    )

    assert [event["turn"] for event in view["events"]] == [1, 1, 1]
    media = view["events"][1]
    assert media["kind"] == "user_input"
    assert media["content"] == [image, missing, caption]
    assert media["content"][0]["image_url"]["url"] == "https://example.com/a.png"
    assert media["unavailable_parts"] == ["content[1].image_url"]
    assert "description" not in media
    assert all("description" not in part for part in media["content"] if isinstance(part, dict))
    assert media["content_match"] == "message_only"


def test_unresolved_external_preview_is_not_complete_content():
    preview = "a,b\n1,2\n..."
    url = "https://storage.example/files/user-1/out.txt"
    stored = f"{preview}\n\n{_MARKER}{url}"
    trace, view = _view(
        {
            "id": "run-large",
            "status": "success",
            "result": {
                "config": {
                    "messages": [
                        {"role": "user", "content": "पढ़"},
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": "{}"},
                                }
                            ],
                        },
                        {"role": "tool", "tool_call_id": "call_1", "content": stored},
                    ]
                },
                "results": [
                    {
                        "input": {"text": "पढ़"},
                        "output": {"blocks": [{"text": stored, "file_ids": ["file-9"]}]},
                    }
                ],
            },
        }
    )

    tool = next(event for event in view["events"] if event["kind"] == "tool_result")
    assert tool["content"] == preview + "\n\n"
    assert tool["truncated"] is True
    assert tool["unresolved"] is True
    assert tool["complete"] is False
    assert tool["missing"] == ["external_content"]
    assert tool["content_source"] == "message"
    assert tool["external"]["file_url"] == url
    assert tool["external"]["retrieval_status"] == "not_attempted"
    assert "available_preview" not in tool
    assert "available_preview" not in tool["external"]
    assert _count(view, preview) == 1
    assert _count(view, _MARKER) == 0
    output = next(event for event in view["events"] if event["kind"] == "assistant_response")
    block = output["output_context"]["blocks"][0]
    assert block["text_omitted"] is True
    assert block["text_match"] == "same_as_tool_message"
    assert block["file_ids"] == ["file-9"]
    assert "text" not in block
    assert _MARKER in trace["result"]["config"]["messages"][2]["content"]


def test_retrieved_external_text_replaces_the_preview():
    preview = "a,b\n1,2\n..."
    url = "https://storage.example/files/user-1/out.txt"
    stored = f"{preview}\n\n{_MARKER}{url}"
    full = "a,b\n1,2\n3,4"
    trace, view = _view(
        {
            "id": "run-retrieved",
            "status": "success",
            "result": {
                "config": {
                    "messages": [
                        {"role": "user", "content": "पढ़"},
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": "{}"},
                                }
                            ],
                        },
                        {"role": "tool", "tool_call_id": "call_1", "content": stored},
                    ]
                },
                "results": [
                    {"input": {"text": "पढ़"}, "output": {"blocks": [{"text": stored}]}}
                ],
            },
        },
        retrieve_external=lambda file_url: full if file_url == url else None,
    )

    tool = next(event for event in view["events"] if event["kind"] == "tool_result")
    assert tool["content"] == full
    assert tool["content_source"] == "retrieved_external"
    assert tool["complete"] is True
    assert "truncated" not in tool
    assert "unresolved" not in tool
    assert "missing" not in tool
    assert "available_preview" not in tool["external"]
    assert "retrieved_text" not in tool["external"]
    assert _count(view, full) == 1
    assert _count(view, preview) == 0
    assert trace["result"]["config"]["messages"][2]["content"] == stored


def test_unmatched_result_entry_keeps_its_own_turn():
    _trace, view = _view(
        {
            "id": "run-unmatched-result",
            "status": "success",
            "result": {
                "config": {
                    "messages": [
                        {"role": "user", "content": "पहला"},
                        {"role": "assistant", "content": "जवाब"},
                    ]
                },
                "results": [
                    {"input": {"text": "पहला"}, "output": {"blocks": [{"text": "जवाब"}]}},
                    {
                        "input": {"text": "यह मैसेज नहीं बना"},
                        "output": {"blocks": [{"text": "खाली"}]},
                    },
                ],
            },
        }
    )

    assert [(event["kind"], event["turn"], event["turn_match"]) for event in view["events"]] == [
        ("user_input", 1, "paired"),
        ("assistant_response", 1, "paired"),
        ("user_input", 2, "unmatched_result"),
        ("assistant_response", 2, "unmatched_result"),
    ]
    unmatched = view["events"][2]
    assert unmatched["content"] == "यह मैसेज नहीं बना"
    assert unmatched["content_source"] == "result"
    assert unmatched["missing"] == ["messages"]
    assert "message_index" not in unmatched
    assert view["events"][3]["content"] == "खाली"
    assert view["events"][3]["missing"] == ["messages"]


def test_results_without_messages_are_kept():
    _trace, view = _view(
        {
            "id": "results-only",
            "status": "error",
            "error_message": {"text": "ValueError: disk full"},
            "result": {
                "config": {"model": "gpt-4.1"},
                "results": [
                    {
                        "input": {"text": "बिना मैसेज", "file_ids": ["file-1"]},
                        "output": {"blocks": [{"text": "नतीजा"}]},
                    }
                ],
            },
        }
    )

    assert view["context"]["status"] == "error"
    assert view["context"]["error_message"] == {"text": "ValueError: disk full"}
    assert view["context"]["model"] == "gpt-4.1"
    user, assistant = view["events"]
    assert user["turn"] == 1
    assert user["content"] == "बिना मैसेज"
    assert user["missing"] == ["messages"]
    assert user["input_context"]["file_ids"] == ["file-1"]
    assert "message_index" not in user
    assert assistant["kind"] == "assistant_response"
    assert assistant["turn"] == 1
    assert assistant["content"] == "नतीजा"
    assert assistant["content_source"] == "result"
    assert assistant["missing"] == ["messages"]


def test_messages_without_results_are_kept():
    _trace, view = _view(
        {
            "id": "messages-only",
            "status": "processing",
            "result": {"config": {"messages": [{"role": "user", "content": "अकेला"}]}},
        }
    )

    assert len(view["events"]) == 1
    user = view["events"][0]
    assert user["turn"] == 1
    assert user["kind"] == "user_input"
    assert user["content"] == "अकेला"
    assert user["missing"] == ["result"]
    assert user["content_match"] == "message_only"
    assert "result_index" not in user
    assert "input_context" not in user
    assert view["context"]["status"] == "processing"


def test_format_does_not_change_the_stage1_trace():
    image = {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
    trace, view = _view(
        {
            "id": "run-frozen",
            "status": "success",
            "result": {
                "config": {
                    "tools": [{"type": "function", "function": {"name": "read", "description": "Read"}}],
                    "messages": [
                        {"role": "user", "content": "देख"},
                        {"role": "user", "content": [image, {"type": "text", "text": "कैप्शन"}]},
                        {"role": "assistant", "content": "देखा"},
                    ],
                },
                "results": [
                    {
                        "input": {"text": "देख", "metadata": {"nested": {"ok": True}}},
                        "output": {"blocks": [{"text": "देखा"}]},
                    }
                ],
            },
        }
    )
    before = copy.deepcopy(trace)

    assert trace == before
    assert format_trace_for_llm(trace) == view
    view["context"]["available_tools"][0]["function"]["description"] = "changed"
    view["events"][1]["content"][0]["image_url"]["url"] = "changed"
    view["events"][0]["input_context"]["metadata"]["nested"]["ok"] = False
    assert trace == before
    assert trace["result"]["config"]["tools"][0]["function"]["description"] == "Read"
    assert trace["result"]["config"]["messages"][1]["content"][0]["image_url"]["url"].endswith("a.png")
