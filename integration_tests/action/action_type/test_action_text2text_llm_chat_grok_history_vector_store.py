import json
from unittest.mock import AsyncMock, patch

import pytest

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_grok import (
    ActionText2TextLlmChatGrok,
    GrokChatConfig,
)
from src.autobots.conn.grok.grok_chat.grok_models import GrokMessage, GrokRole
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.openai_vector_store_tool import OpenAIVectorStoreToolOperation
from src.autobots.user.user_orm_model import UserORM


def _make_action(**config_overrides) -> ActionText2TextLlmChatGrok:
    config = GrokChatConfig(**config_overrides)
    user = UserORM(id="test_user_id", email="test_user@runkai.ai")
    return ActionText2TextLlmChatGrok(action_config=config, user=user, db=None)


def _turn(turn: int, *messages: dict) -> dict:
    return {"turn": turn, "messages": list(messages)}


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}

@pytest.mark.asyncio
async def test_update_config_with_prev_results_is_a_noop():
    config = GrokChatConfig(messages=[GrokMessage(role=GrokRole.user, content="hi")])
    result = await ActionText2TextLlmChatGrok.update_config_with_prev_results(
        Context(), AppState(), config, prev_results=None
    )
    assert result is config

def test_visible_turns_defaults_to_20():
    action = _make_action()
    assert action._visible_turns() == 20


def test_visible_turns_respects_custom_value():
    action = _make_action(visible_turns=5)
    assert action._visible_turns() == 5


def test_ensure_pinned_messages_initialized_captures_turn_1_prefix():
    action = _make_action(
        chat_turn=1,
        messages=[GrokMessage(role=GrokRole.system, content="You are a helpful assistant.")],
    )
    action._ensure_pinned_messages_initialized()
    pinned = action._to_plain(action.action_config.pinned_messages)
    assert len(pinned) == 1
    assert pinned[0]["content"] == "You are a helpful assistant."


def test_ensure_pinned_messages_initialized_noop_after_turn_1():
    action = _make_action(chat_turn=3, messages=[GrokMessage(role=GrokRole.user, content="hi")])
    action._ensure_pinned_messages_initialized()
    assert action.action_config.pinned_messages == []


def test_rebuild_working_messages_from_history_applies_sliding_window():
    turn_history = [_turn(n, _msg("user", f"message {n}")) for n in range(1, 25)]
    action = _make_action(visible_turns=20, turn_history=turn_history)

    action._rebuild_working_messages_from_history()

    rebuilt = action._to_plain(action.action_config.messages)
    # Only the last 20 turns' messages should be present (turns 5-24).
    assert len(rebuilt) == 20
    assert rebuilt[0]["content"] == "message 5"
    assert rebuilt[-1]["content"] == "message 24"


def test_append_current_turn_to_history_captures_delta():
    action = _make_action(
        messages=[
            GrokMessage(role=GrokRole.user, content="what's my cat's name?"),
            GrokMessage(role=GrokRole.assistant, content="Nimbus"),
        ]
    )
    appended = action._append_current_turn_to_history(turn=5, start_index=0)
    assert appended is True

    history = action._to_plain(action.action_config.turn_history)
    assert len(history) == 1
    assert history[0]["turn"] == 5
    assert len(history[0]["messages"]) == 2


def test_minimize_persisted_messages_clears_working_set():
    action = _make_action(messages=[GrokMessage(role=GrokRole.user, content="hi")])
    action._minimize_persisted_messages()
    assert action.action_config.messages == []


def test_ensure_default_tool_selection_adds_history_query_tool():
    action = _make_action()
    action._ensure_default_tool_selection()
    assert "grok_chat_history_query" in action.action_config.tools


def test_ensure_default_tool_selection_does_not_duplicate():
    action = _make_action(tools=["grok_chat_history_query", "some_other_tool"])
    action._ensure_default_tool_selection()
    assert action.action_config.tools.count("grok_chat_history_query") == 1

def test_history_index_every_n_turns_defaults_to_20():
    action = _make_action()
    assert action._history_index_every_n_turns() == 20


def test_history_index_every_n_turns_respects_custom_value():
    action = _make_action(history_vector_store_index_every_n_turns=5)
    assert action._history_index_every_n_turns() == 5


def test_history_vector_store_enabled_defaults_true():
    action = _make_action()
    assert action._history_vector_store_enabled() is True


def test_history_vector_store_enabled_respects_flag():
    action = _make_action(history_vector_store_enabled=False)
    assert action._history_vector_store_enabled() is False

def test_render_turn_history_messages_as_json_includes_turns_and_roles():
    action = _make_action()
    turns = [
        _turn(1, _msg("user", "What is OAuth?"), _msg("assistant", "It's an auth protocol.")),
        _turn(2, _msg("user", "Summarize that in one line")),
    ]

    payload = json.loads(action._render_turn_history_messages_as_json(turns=turns))

    assert payload[0]["turn"] == 1
    assert payload[0]["messages"] == [
        {"role": "user", "content": "What is OAuth?"},
        {"role": "assistant", "content": "It's an auth protocol."},
    ]
    assert payload[1]["messages"] == [{"role": "user", "content": "Summarize that in one line"}]


def test_render_turn_history_messages_as_json_empty_input_returns_empty_array():
    action = _make_action()
    assert action._render_turn_history_messages_as_json(turns=[]) == "[]"


def test_render_turn_history_tool_activity_as_text_is_always_empty():
    action = _make_action()
    turns = [_turn(1, _msg("user", "What is OAuth?"), _msg("assistant", "It's an auth protocol."))]
    assert action._render_turn_history_tool_activity_as_text(turns=turns) == ""

def _vs_datablocks(payload: dict) -> DataBlocks:
    return DataBlocks(blocks=[DataBlock(text=json.dumps(payload))])

@pytest.mark.asyncio
async def test_index_turn_history_batch_uploads_only_messages_file():
    action = _make_action(history_vector_store_id="vs_existing")
    turns = [_turn(1, _msg("user", "What is OAuth?"))]

    fake_file = type("FakeFile", (), {"id": "file_abc"})()
    mock_openai_conn = AsyncMock()
    mock_openai_conn.client = object()

    with (
        patch(
            "src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_grok.AppConn.get_openai",
            new_callable=AsyncMock,
            return_value=mock_openai_conn,
        ),
        patch(
            "src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_grok.OpenaiFiles.create",
            new_callable=AsyncMock,
            return_value=fake_file,
        ),
        patch(
            "src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_grok.openai_vector_store_tool",
            new_callable=AsyncMock,
            return_value=_vs_datablocks({"ok": True, "files": []}),
        ) as mock_vs_tool,
    ):
        await action._index_turn_history_batch_to_vector_store(
            ctx=Context(), notify=None, turns=turns, turn_start=1, turn_end=1
        )

    add_files_call = mock_vs_tool.await_args_list[-1]
    files = add_files_call.kwargs["config"].files
    assert len(files) == 1
    assert files[0].attributes["content_type"] == "messages"

@pytest.mark.asyncio
async def test_replace_does_nothing_when_no_files_listed():
    action = _make_action(history_vector_store_id="vs_existing")

    with patch(
        "src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_grok.openai_vector_store_tool",
        new_callable=AsyncMock,
        return_value=_vs_datablocks({"ok": True, "files": []}),
    ) as mock_vs_tool:
        await action._replace_existing_history_vector_store_files(
            Context(), None, turn_start=1, turn_end=20, content_types={"messages"}
        )

    mock_vs_tool.assert_awaited_once()
    assert mock_vs_tool.await_args.kwargs["config"].operation == OpenAIVectorStoreToolOperation.LIST_VECTOR_STORE_FILES


@pytest.mark.asyncio
async def test_replace_deletes_only_files_matching_exact_range_and_type():
    action = _make_action(history_vector_store_id="vs_existing")
    listed = {
        "ok": True,
        "files": [
            {"id": "file_match", "attributes": {"turn_start": 1, "turn_end": 20, "content_type": "messages"}},
            {"id": "file_other_type", "attributes": {"turn_start": 1, "turn_end": 20, "content_type": "tool_activity"}},
            {"id": "file_other_range", "attributes": {"turn_start": 21, "turn_end": 40, "content_type": "messages"}},
        ],
    }

    with patch(
        "src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_grok.openai_vector_store_tool",
        new_callable=AsyncMock,
        side_effect=[_vs_datablocks(listed), _vs_datablocks({"ok": True})],
    ) as mock_vs_tool:
        await action._replace_existing_history_vector_store_files(
            Context(), None, turn_start=1, turn_end=20, content_types={"messages"}
        )

    assert mock_vs_tool.await_count == 2
    delete_call = mock_vs_tool.await_args_list[1]
    delete_config = delete_call.kwargs["config"]
    assert delete_config.operation == OpenAIVectorStoreToolOperation.DELETE_VECTOR_STORE_FILES
    assert delete_config.delete_file_ids == ["file_match"]

@pytest.mark.asyncio
async def test_maybe_index_skips_when_disabled():
    action = _make_action(history_vector_store_enabled=False)
    action._ensure_history_vector_store_exists = AsyncMock()
    action._index_turn_history_batch_to_vector_store = AsyncMock()

    await action._maybe_index_turn_history_to_vector_store(
        ctx=Context(), notify=None, current_turn=20
    )

    action._ensure_history_vector_store_exists.assert_not_called()
    action._index_turn_history_batch_to_vector_store.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_index_skips_when_turn_not_a_multiple_of_n():
    action = _make_action(history_vector_store_index_every_n_turns=20)
    action._ensure_history_vector_store_exists = AsyncMock()
    action._index_turn_history_batch_to_vector_store = AsyncMock()

    await action._maybe_index_turn_history_to_vector_store(
        ctx=Context(), notify=None, current_turn=19
    )

    action._ensure_history_vector_store_exists.assert_not_called()
    action._index_turn_history_batch_to_vector_store.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_index_triggers_on_20th_turn_and_advances_watermark():
    turn_history = [_turn(n, _msg("user", f"message {n}")) for n in range(1, 21)]
    action = _make_action(
        history_vector_store_index_every_n_turns=20,
        history_vector_store_last_indexed_turn=0,
        turn_history=turn_history,
    )
    action._ensure_history_vector_store_exists = AsyncMock()
    action._index_turn_history_batch_to_vector_store = AsyncMock()

    await action._maybe_index_turn_history_to_vector_store(
        ctx=Context(), notify=None, current_turn=20
    )

    action._ensure_history_vector_store_exists.assert_awaited_once()
    action._index_turn_history_batch_to_vector_store.assert_awaited_once()

    _, kwargs = action._index_turn_history_batch_to_vector_store.await_args
    assert kwargs["turn_start"] == 1
    assert kwargs["turn_end"] == 20
    assert len(kwargs["turns"]) == 20

    assert action.action_config.history_vector_store_last_indexed_turn == 20


@pytest.mark.asyncio
async def test_maybe_index_is_best_effort_and_never_raises():
    """Indexing failures must never break the chat turn itself."""
    turn_history = [_turn(n, _msg("user", f"message {n}")) for n in range(1, 21)]
    action = _make_action(
        history_vector_store_index_every_n_turns=20,
        history_vector_store_last_indexed_turn=0,
        turn_history=turn_history,
    )
    action._ensure_history_vector_store_exists = AsyncMock()
    action._index_turn_history_batch_to_vector_store = AsyncMock(
        side_effect=RuntimeError("openai is down")
    )

    await action._maybe_index_turn_history_to_vector_store(
        ctx=Context(), notify=None, current_turn=20
    )
    assert action.action_config.history_vector_store_last_indexed_turn == 0
