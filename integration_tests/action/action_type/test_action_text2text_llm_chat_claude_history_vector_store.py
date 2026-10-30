"""AUT-1543: conversation-memory vector store indexing (every N completed turns).

Covers the pure trigger/rendering logic in ActionText2TextLlmChatclaude that
periodically indexes turn_history into an OpenAI vector store so
chat_history_query_claude can semantically search older turns. External calls
(OpenAI files/vector-store APIs) are mocked; nothing here hits real services.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_claude import (
    ActionText2TextLlmChatclaude,
    ClaudeChatConfig,
)
from src.autobots.conn.claude.chat_model import ClaudeMessageParam
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.openai_vector_store_tool import OpenAIVectorStoreToolOperation
from src.autobots.user.user_orm_model import UserORM


def _make_action(**config_overrides) -> ActionText2TextLlmChatclaude:
    config = ClaudeChatConfig(
        messages=[ClaudeMessageParam(role="user", content="hello")],
        **config_overrides,
    )
    user = UserORM(id="test_user_id", email="test_user@runkai.ai")
    return ActionText2TextLlmChatclaude(action_config=config, user=user, db=None)


def _turn(turn: int, *messages: dict) -> dict:
    return {"turn": turn, "messages": list(messages)}


# --- _history_index_every_n_turns / _history_vector_store_enabled -----------


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


# --- _render_turn_history_messages_as_json / _render_turn_history_tool_activity_as_text --


def test_render_turn_history_messages_as_json_includes_turns_and_roles():
    action = _make_action()
    turns = [
        _turn(
            1,
            {"role": "user", "content": [{"type": "text", "text": "What is OAuth?"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "It's an auth protocol."}]},
        ),
        _turn(
            2,
            {"role": "user", "content": "Summarize that in one line"},
        ),
    ]

    payload = json.loads(action._render_turn_history_messages_as_json(turns=turns))

    assert payload[0]["turn"] == 1
    assert payload[0]["messages"] == [
        {"role": "user", "content": "What is OAuth?"},
        {"role": "assistant", "content": "It's an auth protocol."},
    ]
    assert payload[1]["messages"] == [{"role": "user", "content": "Summarize that in one line"}]


def test_render_turn_history_messages_as_json_excludes_tool_blocks():
    action = _make_action()
    turns = [
        _turn(
            1,
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "web_search", "input": {"query": "OAuth spec"}}],
            },
        ),
    ]

    payload = json.loads(action._render_turn_history_messages_as_json(turns=turns))

    assert payload == []


def test_render_turn_history_messages_as_json_empty_input_returns_empty_array():
    action = _make_action()
    assert action._render_turn_history_messages_as_json(turns=[]) == "[]"


def test_render_turn_history_tool_activity_as_text_summarizes_tool_blocks():
    action = _make_action()
    turns = [
        _turn(
            1,
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "web_search", "input": {"query": "OAuth spec"}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": [{"type": "text", "text": "RFC 6749 defines OAuth 2.0."}],
                    }
                ],
            },
        ),
    ]

    text = action._render_turn_history_tool_activity_as_text(turns=turns)

    assert "=== Turn 1 ===" in text
    assert "called tool web_search" in text
    assert "OAuth spec" in text
    assert "tool result:" in text
    assert "RFC 6749 defines OAuth 2.0." in text


def test_render_turn_history_tool_activity_as_text_excludes_plain_messages():
    action = _make_action()
    turns = [_turn(1, {"role": "user", "content": "What is OAuth?"})]

    assert action._render_turn_history_tool_activity_as_text(turns=turns) == ""


def test_render_turn_history_tool_activity_as_text_empty_input_returns_empty_string():
    action = _make_action()
    assert action._render_turn_history_tool_activity_as_text(turns=[]) == ""


# --- _index_turn_history_batch_to_vector_store (messages + tool-activity split) --


@pytest.mark.asyncio
async def test_index_turn_history_batch_uploads_two_separate_files():
    action = _make_action(history_vector_store_id="vs_existing")
    turns = [
        _turn(
            1,
            {"role": "user", "content": "What is OAuth?"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "web_search", "input": {"query": "OAuth"}}],
            },
        )
    ]

    fake_file = type("FakeFile", (), {"id": "file_abc"})()
    mock_openai_conn = AsyncMock()
    mock_openai_conn.client = object()

    with (
        patch(
            "src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_claude.AppConn.get_openai",
            new_callable=AsyncMock,
            return_value=mock_openai_conn,
        ),
        patch(
            "src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_claude.OpenaiFiles.create",
            new_callable=AsyncMock,
            return_value=fake_file,
        ),
        patch(
            "src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_claude.openai_vector_store_tool",
            new_callable=AsyncMock,
        ) as mock_vs_tool,
    ):
        action._extract_json_payload_from_datablocks = lambda _res: {"ok": True}

        await action._index_turn_history_batch_to_vector_store(
            ctx=Context(), app_state=AppState(), notify=None, turns=turns, turn_start=1, turn_end=1
        )

    _, kwargs = mock_vs_tool.await_args
    files = kwargs["config"].files
    assert len(files) == 2
    assert {f.attributes["content_type"] for f in files} == {"messages", "tool_activity"}


@pytest.mark.asyncio
async def test_index_turn_history_batch_skips_empty_tool_activity_file():
    action = _make_action(history_vector_store_id="vs_existing")
    turns = [_turn(1, {"role": "user", "content": "What is OAuth?"})]

    fake_file = type("FakeFile", (), {"id": "file_abc"})()
    mock_openai_conn = AsyncMock()
    mock_openai_conn.client = object()

    with (
        patch(
            "src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_claude.AppConn.get_openai",
            new_callable=AsyncMock,
            return_value=mock_openai_conn,
        ),
        patch(
            "src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_claude.OpenaiFiles.create",
            new_callable=AsyncMock,
            return_value=fake_file,
        ),
        patch(
            "src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_claude.openai_vector_store_tool",
            new_callable=AsyncMock,
        ) as mock_vs_tool,
    ):
        action._extract_json_payload_from_datablocks = lambda _res: {"ok": True}

        await action._index_turn_history_batch_to_vector_store(
            ctx=Context(), app_state=AppState(), notify=None, turns=turns, turn_start=1, turn_end=1
        )

    _, kwargs = mock_vs_tool.await_args
    files = kwargs["config"].files
    assert len(files) == 1
    assert files[0].attributes["content_type"] == "messages"


# --- _replace_existing_history_vector_store_files (dedupe on retry/re-index) --


def _vs_datablocks(payload: dict) -> DataBlocks:
    return DataBlocks(blocks=[DataBlock(text=json.dumps(payload))])


@pytest.mark.asyncio
async def test_replace_does_nothing_when_no_files_listed():
    action = _make_action(history_vector_store_id="vs_existing")

    with patch(
        "src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_claude.openai_vector_store_tool",
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
        "src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_claude.openai_vector_store_tool",
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
async def test_index_turn_history_batch_replaces_stale_files_before_upload():
    action = _make_action(history_vector_store_id="vs_existing")
    turns = [_turn(1, {"role": "user", "content": "hello"})]

    fake_file = type("FakeFile", (), {"id": "file_new"})()
    mock_openai_conn = AsyncMock()
    mock_openai_conn.client = object()
    listed = {
        "ok": True,
        "files": [{"id": "file_stale", "attributes": {"turn_start": 1, "turn_end": 1, "content_type": "messages"}}],
    }

    with (
        patch(
            "src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_claude.AppConn.get_openai",
            new_callable=AsyncMock,
            return_value=mock_openai_conn,
        ),
        patch(
            "src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_claude.OpenaiFiles.create",
            new_callable=AsyncMock,
            return_value=fake_file,
        ),
        patch(
            "src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_claude.openai_vector_store_tool",
            new_callable=AsyncMock,
            side_effect=[_vs_datablocks(listed), _vs_datablocks({"ok": True}), _vs_datablocks({"ok": True})],
        ) as mock_vs_tool,
    ):
        await action._index_turn_history_batch_to_vector_store(
            ctx=Context(), app_state=AppState(), notify=None, turns=turns, turn_start=1, turn_end=1
        )

    operations = [call.kwargs["config"].operation for call in mock_vs_tool.await_args_list]
    assert operations == [
        OpenAIVectorStoreToolOperation.LIST_VECTOR_STORE_FILES,
        OpenAIVectorStoreToolOperation.DELETE_VECTOR_STORE_FILES,
        OpenAIVectorStoreToolOperation.ADD_FILES,
    ]


# --- _maybe_index_turn_history_to_vector_store (the "every 20 loops" trigger) --


@pytest.mark.asyncio
async def test_maybe_index_skips_when_disabled():
    action = _make_action(history_vector_store_enabled=False)
    action._ensure_history_vector_store_exists = AsyncMock()
    action._index_turn_history_batch_to_vector_store = AsyncMock()

    await action._maybe_index_turn_history_to_vector_store(
        ctx=Context(), app_state=AppState(), notify=None, current_turn=20
    )

    action._ensure_history_vector_store_exists.assert_not_called()
    action._index_turn_history_batch_to_vector_store.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_index_skips_when_turn_not_a_multiple_of_n():
    action = _make_action(history_vector_store_index_every_n_turns=20)
    action._ensure_history_vector_store_exists = AsyncMock()
    action._index_turn_history_batch_to_vector_store = AsyncMock()

    await action._maybe_index_turn_history_to_vector_store(
        ctx=Context(), app_state=AppState(), notify=None, current_turn=19
    )

    action._ensure_history_vector_store_exists.assert_not_called()
    action._index_turn_history_batch_to_vector_store.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_index_skips_when_already_indexed_up_to_that_turn():
    action = _make_action(
        history_vector_store_index_every_n_turns=20,
        history_vector_store_last_indexed_turn=20,
    )
    action._ensure_history_vector_store_exists = AsyncMock()
    action._index_turn_history_batch_to_vector_store = AsyncMock()

    await action._maybe_index_turn_history_to_vector_store(
        ctx=Context(), app_state=AppState(), notify=None, current_turn=20
    )

    action._ensure_history_vector_store_exists.assert_not_called()
    action._index_turn_history_batch_to_vector_store.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_index_triggers_on_20th_turn_and_advances_watermark():
    turn_history = [
        _turn(n, {"role": "user", "content": [{"type": "text", "text": f"message {n}"}]})
        for n in range(1, 21)
    ]
    action = _make_action(
        history_vector_store_index_every_n_turns=20,
        history_vector_store_last_indexed_turn=0,
        turn_history=turn_history,
    )
    action._ensure_history_vector_store_exists = AsyncMock()
    action._index_turn_history_batch_to_vector_store = AsyncMock()

    await action._maybe_index_turn_history_to_vector_store(
        ctx=Context(), app_state=AppState(), notify=None, current_turn=20
    )

    action._ensure_history_vector_store_exists.assert_awaited_once()
    action._index_turn_history_batch_to_vector_store.assert_awaited_once()

    _, kwargs = action._index_turn_history_batch_to_vector_store.await_args
    assert kwargs["turn_start"] == 1
    assert kwargs["turn_end"] == 20
    assert len(kwargs["turns"]) == 20

    assert action.action_config.history_vector_store_last_indexed_turn == 20


@pytest.mark.asyncio
async def test_maybe_index_only_batches_turns_since_last_indexed():
    # 40 completed turns, but the first 20 were already indexed previously.
    turn_history = [
        _turn(n, {"role": "user", "content": [{"type": "text", "text": f"message {n}"}]})
        for n in range(1, 41)
    ]
    action = _make_action(
        history_vector_store_index_every_n_turns=20,
        history_vector_store_last_indexed_turn=20,
        turn_history=turn_history,
        history_vector_store_id="vs_existing",
    )
    action._ensure_history_vector_store_exists = AsyncMock()
    action._index_turn_history_batch_to_vector_store = AsyncMock()

    await action._maybe_index_turn_history_to_vector_store(
        ctx=Context(), app_state=AppState(), notify=None, current_turn=40
    )

    _, kwargs = action._index_turn_history_batch_to_vector_store.await_args
    assert kwargs["turn_start"] == 21
    assert kwargs["turn_end"] == 40
    assert len(kwargs["turns"]) == 20
    assert all(21 <= t["turn"] <= 40 for t in kwargs["turns"])

    assert action.action_config.history_vector_store_last_indexed_turn == 40


@pytest.mark.asyncio
async def test_maybe_index_is_best_effort_and_never_raises():
    """Indexing failures must never break the chat turn itself."""
    turn_history = [
        _turn(n, {"role": "user", "content": [{"type": "text", "text": f"message {n}"}]})
        for n in range(1, 21)
    ]
    action = _make_action(
        history_vector_store_index_every_n_turns=20,
        history_vector_store_last_indexed_turn=0,
        turn_history=turn_history,
    )
    action._ensure_history_vector_store_exists = AsyncMock()
    action._index_turn_history_batch_to_vector_store = AsyncMock(
        side_effect=RuntimeError("openai is down")
    )

    # Must not raise.
    await action._maybe_index_turn_history_to_vector_store(
        ctx=Context(), app_state=AppState(), notify=None, current_turn=20
    )

    # Watermark must NOT advance on failure, so the batch is retried next time.
    assert action.action_config.history_vector_store_last_indexed_turn == 0
