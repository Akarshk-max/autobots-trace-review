import json

from src.autobots.action_run_trace import (
    build_openrouter_request,
    format_trace_for_llm,
    join_action_run,
    load_skill,
)


def _message_contents(trace: dict) -> list:
    return trace["result"]["config"]["messages"]


def test_multiple_turns_keep_every_run_field_and_full_config():
    document = {
        "id": "run-1",
        "_id": "run-1",
        "name": "main agent",
        "user_id": "user-1",
        "space_id": "space-1",
        "type": "action_run",
        "status": "success",
        "error_message": None,
        "created_at": "2026-01-02T03:04:05Z",
        "updated_at": "2026-01-02T03:09:05Z",
        "file_url": "https://storage.example/action_runs/user-1/run-1.json",
        "batch_id": "batch-1",
        "parent_action_result_id": "parent-1",
        "is_saved": True,
        "queued_inputs": {"inputs": [{"text": "later"}], "processed_count": 0},
        "custom_flag": {"kept": True},
        "result": {
            "id": "action-1",
            "type": "text2text_llm_chat_openai",
            "name": "main agent",
            "config": {
                "provider": "openai",
                "model": "gpt-4.1",
                "tools": [{"type": "function", "function": {"name": "run_terminal_command"}}],
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": "You are the main agent."},
                    {"role": "user", "content": "पहला काम"},
                    {"role": "assistant", "content": "पहला जवाब"},
                    {"role": "user", "content": "दूसरा काम"},
                    {"role": "assistant", "content": "दूसरा जवाब"},
                ],
            },
            "results": [
                {"input": {"text": "पहला काम"}, "output": {"blocks": [{"text": "पहला जवाब"}]}},
                {"input": {"text": "दूसरा काम"}, "output": {"blocks": [{"text": "दूसरा जवाब"}]}},
            ],
        },
    }

    trace = join_action_run(document)

    assert trace["run"]["name"] == "main agent"
    assert trace["run"]["user_id"] == "user-1"
    assert trace["run"]["space_id"] == "space-1"
    assert trace["run"]["status"] == "success"
    assert trace["run"]["error_message"] is None
    assert trace["run"]["created_at"] == "2026-01-02T03:04:05Z"
    assert trace["run"]["updated_at"] == "2026-01-02T03:09:05Z"
    assert trace["run"]["file_url"].endswith("run-1.json")
    assert trace["run"]["batch_id"] == "batch-1"
    assert trace["run"]["parent_action_result_id"] == "parent-1"
    assert trace["run"]["queued_inputs"]["inputs"][0]["text"] == "later"
    assert trace["run"]["custom_flag"] == {"kept": True}
    assert trace["run"]["_id"] == "run-1"
    config = trace["result"]["config"]
    assert config["provider"] == "openai"
    assert config["model"] == "gpt-4.1"
    assert config["tools"][0]["function"]["name"] == "run_terminal_command"
    assert config["temperature"] == 0.2
    assert [turn["result_index"] for turn in trace["execution"]["turns"]] == [None, 0, 1]
    assert trace["execution"]["turns"][0]["match"] == "unmatched_messages"
    assert trace["execution"]["turns"][1]["message_indexes"] == [1, 2]
    assert trace["execution"]["turns"][2]["message_indexes"] == [3, 4]
    document["result"]["config"]["model"] = "changed"
    assert trace["result"]["config"]["model"] == "gpt-4.1"


def test_one_turn_keeps_every_assistant_message_and_pairs_tools():
    document = {
        "id": "run-tools",
        "status": "success",
        "result": {
            "type": "text2text_llm_chat_openai",
            "config": {
                "messages": [
                    {"role": "user", "content": "करो"},
                    {
                        "role": "assistant",
                        "content": "शुरू",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "run_terminal_command", "arguments": "{\"command\":\"cat\"}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": "file body"},
                    {"role": "assistant", "content": "हो गया"},
                ]
            },
            "results": [
                {
                    "input": {"text": "करो"},
                    "output": {"blocks": [{"text": "हो गया", "urls": ["https://example.com/out.csv"]}]},
                }
            ],
        },
    }

    trace = join_action_run(document)
    turn = trace["execution"]["turns"][0]

    assert turn["match"] == "paired"
    assert turn["message_indexes"] == [0, 1, 2, 3]
    assert [message["content"] for message in _message_contents(trace) if message["role"] == "assistant"] == [
        "शुरू",
        "हो गया",
    ]
    assert trace["result"]["results"][0]["output"]["blocks"][0]["urls"] == ["https://example.com/out.csv"]
    assert trace["execution"]["tool_links"] == [
        {
            "call_id": "call_1",
            "call_message_index": 1,
            "call_index": 0,
            "result_message_index": 2,
            "status": "paired",
        }
    ]


def test_unmatched_tool_call_and_tool_result_stay_visible():
    document = {
        "id": "run-unmatched-tools",
        "status": "success",
        "result": {
            "config": {
                "messages": [
                    {"role": "user", "content": "करो"},
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": "call_missing", "type": "function", "function": {"name": "a", "arguments": "{}"}}
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_orphan", "content": "no call"},
                ]
            },
            "results": [{"input": {"text": "करो"}, "output": {"blocks": []}}],
        },
    }

    links = join_action_run(document)["execution"]["tool_links"]

    assert links[0]["status"] == "unmatched_call"
    assert links[0]["call_id"] == "call_missing"
    assert links[0]["result_message_index"] is None
    assert links[1]["status"] == "unmatched_result"
    assert links[1]["call_id"] == "call_orphan"
    assert links[1]["call_message_index"] is None


def test_unmatched_result_entry_is_kept_beside_the_paired_turn():
    document = {
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
                {"input": {"text": "यह मैसेज नहीं बना"}, "output": {"blocks": [{"text": "खाली"}]}},
            ],
        },
    }

    turns = join_action_run(document)["execution"]["turns"]

    assert [(turn["result_index"], turn["match"]) for turn in turns] == [
        (0, "paired"),
        (1, "unmatched_result"),
    ]
    assert turns[1]["message_indexes"] == []


def test_image_parts_files_urls_and_metadata_stay_on_the_source_objects():
    image = {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
    document = {
        "id": "run-media",
        "status": "success",
        "result": {
            "config": {
                "messages": [
                    {"role": "user", "content": "देख"},
                    {"role": "user", "content": [image, {"type": "text", "text": "कैप्शन"}]},
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
                    },
                    "output": {
                        "blocks": [
                            {
                                "text": "देखा",
                                "urls": ["https://example.com/out.csv"],
                                "file_ids": ["file-2"],
                            }
                        ]
                    },
                }
            ],
        },
    }

    trace = join_action_run(document)
    stored_input = trace["result"]["results"][0]["input"]
    stored_image = trace["result"]["config"]["messages"][1]["content"][0]

    assert stored_image == image
    assert stored_input["files"] == [{"filename": "data.txt", "content": "a,b"}]
    assert stored_input["file_ids"] == ["file-1"]
    assert stored_input["urls"] == ["https://example.com/data.txt"]
    assert stored_input["metadata"]["process_attachment_url"] is True
    assert stored_input["search_data"]["sources"][0]["id"] == "s1"
    assert stored_input["mentions"] == ["ada@example.com"]
    assert stored_input["user_id"] == "user-1"
    assert trace["result"]["results"][0]["output"]["blocks"][0]["file_ids"] == ["file-2"]
    assert trace["execution"]["turns"][0]["message_indexes"] == [0, 1, 2]


def test_error_status_and_error_message_are_copied():
    document = {
        "id": "run-error",
        "status": "error",
        "error_message": {"text": "ValueError: disk full"},
        "result": {"config": {"messages": []}, "results": []},
    }

    trace = join_action_run(document)

    assert trace["run"]["status"] == "error"
    assert trace["run"]["error_message"] == {"text": "ValueError: disk full"}


def test_large_tool_output_keeps_the_reference_until_it_is_retrieved():
    preview = "a,b\n1,2\n..."
    url = "https://storage.example/files/user-1/out.txt"
    content = f"{preview}\n\nOutput stored in file url: {url}"
    document = {
        "id": "run-large",
        "status": "success",
        "result": {
            "config": {
                "messages": [
                    {"role": "user", "content": "पढ़"},
                    {"role": "tool", "tool_call_id": "call_1", "content": content},
                ]
            },
            "results": [{"input": {"text": "पढ़"}, "output": {"blocks": [{"text": content, "urls": [url]}]}}],
        },
    }

    unresolved = join_action_run(document)
    assert unresolved["result"]["config"]["messages"][1]["content"] == content
    assert unresolved["external_references"][0]["available_preview"] == preview + "\n\n"
    assert unresolved["external_references"][0]["file_url"] == url
    assert unresolved["external_references"][0]["retrieval_status"] == "not_attempted"
    assert unresolved["external_references"][0]["complete"] is False
    assert all(item["complete"] is False for item in unresolved["external_references"])

    resolved = join_action_run(document, retrieve_external=lambda file_url: "a,b\n1,2\n3,4" if file_url == url else None)
    retrieved = [item for item in resolved["external_references"] if item["retrieval_status"] == "retrieved"]
    assert retrieved
    assert retrieved[0]["retrieved_text"] == "a,b\n1,2\n3,4"
    assert retrieved[0]["complete"] is True
    assert resolved["result"]["config"]["messages"][1]["content"] == content


def test_messages_without_results_and_results_without_messages_are_both_kept():
    messages_only = join_action_run(
        {
            "id": "messages-only",
            "status": "processing",
            "result": {"config": {"messages": [{"role": "user", "content": "अकेला"}]}},
        }
    )
    results_only = join_action_run(
        {
            "id": "results-only",
            "status": "success",
            "result": {
                "config": {},
                "results": [{"input": {"text": "बिना मैसेज"}, "output": {"blocks": [{"text": "नतीजा"}]}}],
            },
        }
    )

    assert messages_only["execution"]["turns"] == [
        {
            "turn": 1,
            "result_index": None,
            "match": "unmatched_messages",
            "user_message_index": None,
            "message_indexes": [0],
        }
    ]
    assert messages_only["result"]["config"]["messages"][0]["content"] == "अकेला"
    assert results_only["execution"]["turns"] == [
        {
            "turn": 1,
            "result_index": 0,
            "match": "unmatched_result",
            "user_message_index": None,
            "message_indexes": [],
        }
    ]
    assert results_only["result"]["results"][0]["output"]["blocks"][0]["text"] == "नतीजा"


def test_openrouter_request_still_wraps_the_trace_without_changing_the_skill():
    document = {"id": "run-1", "status": "success", "result": {"config": {"messages": []}, "results": []}}
    request = build_openrouter_request(document, model="meta-llama/llama-3.1-8b-instruct")

    assert request["messages"][0]["content"] == load_skill()
    formatted = format_trace_for_llm(join_action_run(document))
    assert json.loads(request["messages"][1]["content"]) == formatted
