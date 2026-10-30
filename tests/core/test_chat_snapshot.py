from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, Field

from src.autobots.action.action_type.action_map import ACTION_MAP
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.core.cancellation.run_cancellation import RunCancellation
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.chat_snapshot.chat_snapshot import CANCELLED_NOTICE, apply_to_result
from src.autobots.llm.chat_snapshot.provider_snapshots import (
    snapshot_claude,
    snapshot_deepseek,
    snapshot_gemini,
    snapshot_grok,
    snapshot_openai_chat,
    snapshot_openai_responses,
    snapshot_openrouter,
)


class FakeChatConfig(BaseModel):
    model: str = "test-model"
    messages: list[dict[str, Any]] = Field(default_factory=list)


class FakeClaudeReq(BaseModel):
    messages: list[dict[str, Any]] = Field(default_factory=list)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)


class FakeResponsesConfig(BaseModel):
    model: str = "test-model"
    input: list[dict[str, Any]] = Field(default_factory=list)


def _state_with_token() -> tuple[AppState, RunCancellation]:
    token = RunCancellation(run_id="run1")
    app_state = AppState(action_id="a1", action_run_id="run1")
    app_state.set_cancellation(token)
    return app_state, token


def _result_doc() -> SimpleNamespace:
    return SimpleNamespace(
        id="run1",
        result=SimpleNamespace(config={}, output={}, results=[SimpleNamespace(output=None)]),
    )


def test_store_is_a_no_op_without_a_token():
    app_state = AppState(action_id="a1")
    snapshot_openrouter(Context(), app_state, FakeChatConfig(), 1)


def test_snapshot_is_detached_from_the_live_config():
    app_state, token = _state_with_token()
    config = FakeChatConfig(messages=[{"role": "user", "content": "hi"}])

    snapshot_openrouter(Context(), app_state, config, 3)

    config.messages.append({"role": "assistant", "content": "half-written"})
    config.messages[0]["content"] = "mutated"

    assert token.snapshot.messages == [{"role": "user", "content": "hi"}]
    assert token.snapshot.loop_index == 3


def test_later_snapshot_replaces_the_earlier_one():
    app_state, token = _state_with_token()
    config = FakeChatConfig(messages=[{"role": "user", "content": "hi"}])

    for loop_index in range(1, 13):
        config.messages.append({"role": "assistant", "content": f"turn {loop_index}"})
        snapshot_openrouter(Context(), app_state, config, loop_index)

    assert token.snapshot.loop_index == 12
    assert token.snapshot.messages[-1] == {"role": "assistant", "content": "turn 12"}


@pytest.mark.parametrize(
    "snapshot_fn,config,expected",
    [
        (snapshot_openrouter, FakeChatConfig(), {"role": "assistant", "content": CANCELLED_NOTICE}),
        (snapshot_openai_chat, FakeChatConfig(), {"role": "assistant", "content": CANCELLED_NOTICE}),
        (
            snapshot_claude,
            FakeChatConfig(),
            {"role": "assistant", "content": [{"type": "text", "text": CANCELLED_NOTICE}]},
        ),
        (
            snapshot_gemini,
            FakeChatConfig(),
            {"role": "model", "parts": [{"text": CANCELLED_NOTICE}]},
        ),
    ],
)
def test_notice_uses_each_provider_message_format(snapshot_fn, config, expected):
    app_state, token = _state_with_token()
    snapshot_fn(Context(), app_state, config, 1)

    finalized = token.snapshot.finalized()
    assert finalized.messages[-1] == expected


def test_responses_notice_appends_to_input_items():
    app_state, token = _state_with_token()
    snapshot_openai_responses(Context(), app_state, FakeResponsesConfig(), 1)

    finalized = token.snapshot.finalized()
    assert finalized.messages[-1] == {
        "role": "assistant",
        "content": CANCELLED_NOTICE,
        "type": "message",
    }


def test_finalizing_does_not_mutate_the_stored_snapshot():
    app_state, token = _state_with_token()
    snapshot_openrouter(Context(), app_state, FakeChatConfig(), 1)

    token.snapshot.finalized()
    token.snapshot.finalized()

    assert token.snapshot.messages == []


def test_apply_to_result_writes_config_output_and_last_turn():
    app_state, token = _state_with_token()
    config = FakeChatConfig(messages=[{"role": "user", "content": "hi"}])
    snapshot_openrouter(Context(), app_state, config, 12)
    doc = _result_doc()

    assert apply_to_result(Context(), doc, app_state) is True
    assert doc.result.config["turn_history"][-1]["messages"][-1]["content"] == CANCELLED_NOTICE
    assert CANCELLED_NOTICE in doc.result.output["blocks"][0]["text"]
    assert doc.result.results[-1].output == doc.result.output


def test_apply_to_result_still_marks_output_when_nothing_was_captured():
    app_state, _ = _state_with_token()
    doc = _result_doc()

    assert apply_to_result(Context(), doc, app_state) is False
    assert doc.result.config == {}
    assert doc.result.output["blocks"][0]["text"] == CANCELLED_NOTICE
    assert doc.result.results[-1].output == doc.result.output


def test_apply_to_result_marks_output_for_an_action_with_no_loop():
    app_state = AppState(action_id="a1")
    app_state.set_cancellation(RunCancellation(run_id="run1"))
    doc = _result_doc()
    sent = []

    assert apply_to_result(Context(), doc, app_state, notify=lambda ctx, m: sent.append(m)) is False
    assert doc.result.output["blocks"][0]["text"] == CANCELLED_NOTICE
    assert sent == [CANCELLED_NOTICE]


def test_apply_to_result_notifies_once_with_the_reached_step():
    app_state, _ = _state_with_token()
    snapshot_openrouter(Context(), app_state, FakeChatConfig(), 12)
    sent = []

    apply_to_result(Context(), _result_doc(), app_state, notify=lambda ctx, msg: sent.append(msg))
    assert len(sent) == 1
    assert "12" in sent[0]


def test_apply_to_result_survives_a_broken_snapshot():
    app_state, token = _state_with_token()
    snapshot_openrouter(Context(), app_state, FakeChatConfig(), 5)

    def explode(config, notice):
        raise RuntimeError("unserializable config")

    token.snapshot.append_notice = explode
    doc = _result_doc()

    assert apply_to_result(Context(), doc, app_state) is False
    assert doc.result.config == {}
    assert doc.result.output["blocks"][0]["text"] == CANCELLED_NOTICE


def test_apply_to_result_preserves_config_fields_the_request_never_carried():

    app_state, _ = _state_with_token()
    snapshot_claude(Context(), app_state, FakeChatConfig(messages=[{"role": "user"}]), 4)

    doc = _result_doc()
    doc.result.config = {
        "model": "claude-x",
        "messages": [{"role": "user", "content": "old"}],
        "generated_files": ["file-1", "file-2"],
        "tools": ["read_files", "terminal"],
        "supports_image": True,
    }

    assert apply_to_result(Context(), doc, app_state) is True
    assert doc.result.config["generated_files"] == ["file-1", "file-2"]
    assert doc.result.config["tools"] == ["read_files", "terminal"]
    assert doc.result.config["supports_image"] is True
    assert doc.result.config["model"] == "claude-x"
    assert doc.result.config["turn_history"][-1]["messages"][-1]["content"] == [
        {"type": "text", "text": CANCELLED_NOTICE}
    ]


def test_responses_snapshot_targets_the_input_field():
    app_state, _ = _state_with_token()
    snapshot_openai_responses(Context(), app_state, FakeResponsesConfig(input=[{"role": "user"}]), 2)

    doc = _result_doc()
    doc.result.config = {"model": "gpt-x", "input": [{"role": "user"}], "tools": ["t"]}

    assert apply_to_result(Context(), doc, app_state) is True
    assert doc.result.config["tools"] == ["t"]
    assert len(doc.result.config["input"]) == 2


def test_snapshot_can_be_cleared_between_runs_on_a_shared_token():
    app_state, token = _state_with_token()
    snapshot_openrouter(Context(), app_state, FakeChatConfig(messages=[{"role": "user"}]), 7)
    assert token.snapshot is not None

    token.set_snapshot(None)

    doc = _result_doc()
    doc.result.config = {"messages": [{"role": "user", "content": "second run"}]}
    assert apply_to_result(Context(), doc, app_state) is False
    assert doc.result.config["messages"] == [{"role": "user", "content": "second run"}]


PROVIDER_CONFIGS = [
    (ActionType.text2text_llm_responses_openai, snapshot_openai_responses,
     {"input": [], "provider": "openai"}),
    (ActionType.openai_responses_v2, snapshot_openai_responses,
     {"input": [], "provider": "openai"}),
    (ActionType.text2text_llm_chat_openai, snapshot_openai_chat,
     {"messages": [], "provider": "openai"}),
    (ActionType.text2text_llm_chat_claude, snapshot_claude, {"messages": []}),
    (ActionType.text2text_llm_chat_gemini, snapshot_gemini, {}),
    (ActionType.text2text_llm_chat_openrouter, snapshot_openrouter, {}),
    (ActionType.text2text_llm_chat_deepseek, snapshot_deepseek, {}),
    (ActionType.text2text_llm_chat_grok, snapshot_grok, {}),
    (ActionType.text2text_llm_chat_grok_collections, snapshot_grok, {}),
]


@pytest.mark.parametrize("action_type,snapshot_fn,payload", PROVIDER_CONFIGS)
def test_cancellation_notice_survives_action_config_revalidation(
    action_type, snapshot_fn, payload
):

    config_type = ACTION_MAP[action_type].get_config_type()
    config = config_type.model_validate(payload)

    app_state, _ = _state_with_token()
    snapshot_fn(Context(), app_state, config, 1)

    doc = _result_doc()
    doc.result.config = config.model_dump(exclude_none=True, mode="json")
    assert apply_to_result(Context(), doc, app_state) is True

    config_type.model_validate(doc.result.config)


def test_claude_notice_does_not_break_user_assistant_alternation():

    app_state, _ = _state_with_token()
    config = FakeChatConfig(
        messages=[
            {"role": "user", "content": "do the thing"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]},
        ]
    )
    snapshot_claude(Context(), app_state, config, 2)

    messages = _state_snapshot(app_state).finalized().messages
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert not any(a == b == "assistant" for a, b in zip(roles, roles[1:]))


def _state_snapshot(app_state):
    return app_state.cancellation.snapshot


@pytest.mark.parametrize(
    "snapshot_fn,expected_notice",
    [
        (snapshot_gemini, {"role": "model", "parts": [{"text": CANCELLED_NOTICE}]}),
        (snapshot_grok, {"role": "assistant", "content": CANCELLED_NOTICE}),
    ],
)
def test_turn_history_providers_persist_the_cancelled_turn(snapshot_fn, expected_notice):
    token = RunCancellation(run_id="run1")
    token.set_snapshot_meta(turn_start_index=2, chat_turn=3)
    app_state = AppState(action_id="a1", action_run_id="run1")
    app_state.set_cancellation(token)

    config = FakeChatConfig(
        messages=[
            {"role": "user", "content": "older turn"},
            {"role": "assistant", "content": "older reply"},
            {"role": "user", "content": "fetch my repos"},
            {"role": "assistant", "content": "working on it"},
        ]
    )
    snapshot_fn(Context(), app_state, config, 2)

    doc = _result_doc()
    doc.result.config = {
        "messages": [],
        "turn_history": [{"turn": 1, "messages": [{"role": "user", "content": "older turn"}]}],
        "chat_turn": 3,
    }

    assert apply_to_result(Context(), doc, app_state) is True
    history = doc.result.config["turn_history"]
    assert len(history) == 2
    assert history[1]["turn"] == 3
    assert history[1]["messages"][0]["content"] == "fetch my repos"
    assert history[1]["messages"][-1] == expected_notice
    assert doc.result.config["messages"] == []


def test_claude_turn_history_holds_only_text_user_assistant_pairs():
    token = RunCancellation(run_id="run1")
    token.set_snapshot_meta(turn_start_index=2, chat_turn=2)
    app_state = AppState(action_id="a1", action_run_id="run1")
    app_state.set_cancellation(token)

    live = FakeClaudeReq(
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "older"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "older reply"}]},
            {"role": "user", "content": [{"type": "text", "text": "fetch my repos"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check."},
                    {"type": "tool_use", "id": "t1", "name": "api_call", "input": {}},
                ],
            },
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]},
        ],
        tool_results=[{"tool_use_id": "t1", "name": "api_call", "output_text": "200 ok"}],
    )
    snapshot_claude(Context(), app_state, live, 3)

    doc = _result_doc()
    doc.result.config = {
        "messages": [],
        "chat_turn": 2,
        "turn_history": [{"turn": 1, "messages": [{"role": "user", "content": "older"}]}],
        "tool_results": [{"tool_use_id": "t0", "name": "prev", "output_text": "earlier"}],
    }

    assert apply_to_result(Context(), doc, app_state) is True

    turn = doc.result.config["turn_history"][-1]
    assert turn["turn"] == 2
    assert [m["role"] for m in turn["messages"]] == ["user", "assistant"]
    assert {b["type"] for m in turn["messages"] for b in m["content"]} == {"text"}
    assert turn["messages"][-1]["content"][0]["text"] == f"Let me check.\n\n{CANCELLED_NOTICE}"
    assert doc.result.config["messages"] == []


def test_claude_cancel_merges_tool_results_and_dedupes():
    token = RunCancellation(run_id="run1")
    token.set_snapshot_meta(turn_start_index=0, chat_turn=2)
    app_state = AppState(action_id="a1", action_run_id="run1")
    app_state.set_cancellation(token)

    snapshot_claude(
        Context(),
        app_state,
        FakeClaudeReq(
            messages=[{"role": "user", "content": [{"type": "text", "text": "go"}]}],
            tool_results=[
                {"tool_use_id": "t0", "name": "prev", "output_text": "dupe"},
                {"tool_use_id": "t1", "name": "api_call", "output_text": "200"},
                {"name": "no_id_dropped"},
            ],
        ),
        1,
    )

    doc = _result_doc()
    doc.result.config = {
        "messages": [],
        "turn_history": [],
        "tool_results": [{"tool_use_id": "t0", "name": "prev", "output_text": "earlier"}],
    }

    assert apply_to_result(Context(), doc, app_state) is True
    assert [r["tool_use_id"] for r in doc.result.config["tool_results"]] == ["t0", "t1"]
    assert doc.result.config["tool_results"][0]["output_text"] == "earlier"


def test_capture_never_breaks_the_run_it_observes():

    class Unpicklable:
        def __reduce__(self):
            raise TypeError("cannot pickle 'ValidatorIterator' object")

    app_state, token = _state_with_token()
    config = FakeChatConfig(
        messages=[{"role": "user", "content": "ok"}, {"role": "assistant", "content": Unpicklable()}]
    )

    snapshot_claude(Context(), app_state, config, 1)

    assert token.snapshot is not None
    assert token.snapshot.messages[0] == {"role": "user", "content": "ok"}
    assert len(token.snapshot.messages) == 2


CALL = {
    "type": "function_call",
    "name": "search",
    "call_id": "c1",
    "arguments": "{}",
    "id": "fc_1",
    "status": "completed",
}
CALL_OUTPUT = {"type": "function_call_output", "call_id": "c1", "output": "res1"}


class FakeResponsesReq(BaseModel):
    input: list[Any] = Field(default_factory=list)
    input_archive: list[dict[str, Any]] = Field(default_factory=list)


def test_responses_cancel_keeps_the_turns_intermediate_tool_calls():
    """The live request accumulates tool calls into input_archive[-1].in_turn_messages.

    The persisted result config is a separate dict captured before the turn, so
    reading the archive from there dropped every intermediate step: the next turn
    rebuilt to just the user prompt plus the cancellation notice.
    """
    app_state, _ = _state_with_token()
    live = FakeResponsesReq(
        input=[{"role": "user", "content": "long task"}, CALL, CALL_OUTPUT],
        input_archive=[
            {
                "turn_number": 1,
                "input": [{"role": "user", "content": "hi"}],
                "output": [{"role": "assistant", "content": "hello", "type": "message"}],
                "in_turn_messages": [],
            },
            {
                "turn_number": 2,
                "input": [{"role": "user", "content": "long task"}],
                "output": [],
                "in_turn_messages": [CALL, CALL_OUTPUT],
            },
        ],
    )
    snapshot_openai_responses(Context(), app_state, live, 3)

    doc = _result_doc()
    doc.result.config = {
        "provider": "openai",
        "input": [],
        "input_archive": [
            {
                "turn_number": 1,
                "input": [{"role": "user", "content": "hi"}],
                "output": [{"role": "assistant", "content": "hello", "type": "message"}],
                "in_turn_messages": [],
            }
        ],
    }

    assert apply_to_result(Context(), doc, app_state) is True

    archive = doc.result.config["input_archive"]
    assert [e["turn_number"] for e in archive] == [1, 2]
    assert archive[1]["in_turn_messages"] == [CALL, CALL_OUTPUT]
    assert archive[1]["output"] == [
        {"role": "assistant", "content": CANCELLED_NOTICE, "type": "message"}
    ]

    rebuilt = []
    for entry in archive:
        for bucket in (entry.get("input"), entry.get("in_turn_messages"), entry.get("output")):
            rebuilt += list(bucket or [])
    assert [m.get("type") or m.get("role") for m in rebuilt] == [
        "user",
        "message",
        "user",
        "function_call",
        "function_call_output",
        "message",
    ]

    config_type = ACTION_MAP[ActionType.text2text_llm_responses_openai].get_config_type()
    config_type.model_validate({**doc.result.config, "input": rebuilt})


def test_responses_cancel_lands_in_the_archive_not_just_input():
    app_state, _ = _state_with_token()
    snapshot_openai_responses(
        Context(), app_state, FakeResponsesConfig(input=[{"role": "user", "content": "go"}]), 2
    )

    doc = _result_doc()
    doc.result.config = {
        "input": [{"role": "user", "content": "go"}],
        "input_archive": [
            {"turn_number": 1, "input": [{"role": "user", "content": "older"}], "output": []},
            {"turn_number": 2, "input": [{"role": "user", "content": "go"}], "output": []},
        ],
        "visible_turns": 15,
    }

    assert apply_to_result(Context(), doc, app_state) is True

    archive = doc.result.config["input_archive"]
    assert len(archive) == 2
    assert archive[0]["output"] == []
    assert archive[1]["output"] == [
        {"role": "assistant", "content": CANCELLED_NOTICE, "type": "message"}
    ]


def test_openrouter_cancel_does_not_wipe_persisted_messages():
    token = RunCancellation(run_id="run1")
    token.set_snapshot_meta(turn_start_index=2, chat_turn=2)
    app_state = AppState(action_id="a1", action_run_id="run1")
    app_state.set_cancellation(token)

    live = FakeChatConfig(
        messages=[
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
            {"role": "user", "content": "long task"},
            {"role": "tool", "tool_call_id": "c1", "content": "res1"},
        ]
    )
    snapshot_openrouter(Context(), app_state, live, 3)

    doc = _result_doc()
    doc.result.config = {
        "messages": [{"role": "system", "content": "you are helpful"}],
        "pinned_messages": [{"role": "system", "content": "you are helpful"}],
        "turn_history": [{"turn": 1, "messages": [{"role": "user", "content": "hi"}]}],
        "chat_turn": 2,
    }

    assert apply_to_result(Context(), doc, app_state) is True

    assert doc.result.config["messages"] != []
    assert [m["role"] for m in doc.result.config["messages"]] == [
        "system",
        "user",
        "user",
        "tool",
        "assistant",
    ]
    history = doc.result.config["turn_history"]
    assert [t["turn"] for t in history] == [1, 2]
    assert [m["role"] for m in history[1]["messages"]] == ["user", "tool", "assistant"]
    assert history[1]["messages"][-1]["content"] == CANCELLED_NOTICE


@pytest.mark.parametrize("snapshot_fn", [snapshot_claude, snapshot_gemini, snapshot_grok])
def test_wiping_providers_still_clear_messages(snapshot_fn):
    """claude, gemini and grok all call minimize_persisted_messages on success."""
    token = RunCancellation(run_id="run1")
    token.set_snapshot_meta(turn_start_index=1, chat_turn=2)
    app_state = AppState(action_id="a1", action_run_id="run1")
    app_state.set_cancellation(token)

    snapshot_fn(
        Context(),
        app_state,
        FakeChatConfig(messages=[{"role": "user", "content": "hi"}, {"role": "user", "content": "go"}]),
        1,
    )

    doc = _result_doc()
    doc.result.config = {"messages": [{"role": "user", "content": "hi"}], "turn_history": []}

    assert apply_to_result(Context(), doc, app_state) is True
    assert doc.result.config["messages"] == []
    assert len(doc.result.config["turn_history"]) == 1
