import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.conn.gemini.chat_model_gemini import GeminiChatConfig
from src.autobots.conn.gemini.gemini_chat import GeminiChatRuntime
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.openai_vector_store_tool import OpenAIVectorStoreToolOperation
from src.autobots.user.user_orm_model import UserORM


def _make_runtime() -> GeminiChatRuntime:
    user = UserORM(id="test_user_id", email="test_user@runkai.ai")
    return GeminiChatRuntime(user=user, db=None, gemini_client=MagicMock())

def _turn(turn: int, *messages: dict) -> dict:
    return {"turn": turn, "messages": list(messages)}

def test_history_index_every_n_turns_defaults_to_20():
    config = GeminiChatConfig()
    assert GeminiChatRuntime._history_index_every_n_turns(config) == 20

def test_history_index_every_n_turns_respects_custom_value():
    config = GeminiChatConfig(history_vector_store_index_every_n_turns=5)
    assert GeminiChatRuntime._history_index_every_n_turns(config) == 5


def test_history_vector_store_enabled_defaults_true():
    config = GeminiChatConfig()
    assert GeminiChatRuntime._history_vector_store_enabled(config) is True

def test_history_vector_store_enabled_respects_flag():
    config = GeminiChatConfig(history_vector_store_enabled=False)
    assert GeminiChatRuntime._history_vector_store_enabled(config) is False

def test_render_turn_history_messages_as_json_includes_turns_and_roles():
    turns = [
        _turn(
            1,
            {"role": "user", "parts": [{"text": "What is OAuth?"}]},
            {"role": "model", "parts": [{"text": "It's an auth protocol."}]},
        ),
        _turn(
            2,
            {"role": "user", "parts": [{"text": "Summarize that in one line"}]},
        ),
    ]

    payload = json.loads(GeminiChatRuntime._render_turn_history_messages_as_json(turns=turns))

    assert payload[0]["turn"] == 1
    assert payload[0]["messages"] == [
        {"role": "user", "content": "What is OAuth?"},
        {"role": "model", "content": "It's an auth protocol."},
    ]
    assert payload[1]["messages"] == [{"role": "user", "content": "Summarize that in one line"}]

def test_render_turn_history_messages_as_json_excludes_tool_parts():
    turns = [
        _turn(
            1,
            {
                "role": "model",
                "parts": [{"function_call": {"name": "web_search", "args": {"query": "OAuth spec"}}}],
            },
        ),
    ]
    payload = json.loads(GeminiChatRuntime._render_turn_history_messages_as_json(turns=turns))
    assert payload == []

def test_render_turn_history_messages_as_json_empty_input_returns_empty_array():
    assert GeminiChatRuntime._render_turn_history_messages_as_json(turns=[]) == "[]"

def test_render_turn_history_tool_activity_as_text_summarizes_function_call_and_response():
    turns = [
        _turn(
            1,
            {
                "role": "model",
                "parts": [{"function_call": {"name": "web_search", "args": {"query": "OAuth spec"}}}],
            },
            {
                "role": "user",
                "parts": [{"function_response": {"response": {"text": "RFC 6749 defines OAuth 2.0."}}}],
            },
        ),
    ]

    text = GeminiChatRuntime._render_turn_history_tool_activity_as_text(turns=turns)
    assert "=== Turn 1 ===" in text
    assert "called tool web_search" in text
    assert "OAuth spec" in text
    assert "tool result:" in text
    assert "RFC 6749 defines OAuth 2.0." in text

def test_render_turn_history_tool_activity_as_text_excludes_plain_messages():
    turns = [_turn(1, {"role": "user", "parts": [{"text": "What is OAuth?"}]})]
    assert GeminiChatRuntime._render_turn_history_tool_activity_as_text(turns=turns) == ""

def test_render_turn_history_tool_activity_as_text_empty_input_returns_empty_string():
    assert GeminiChatRuntime._render_turn_history_tool_activity_as_text(turns=[]) == ""

def _vs_datablocks(payload: dict) -> DataBlocks:
    return DataBlocks(blocks=[DataBlock(text=json.dumps(payload))])

@pytest.mark.asyncio
async def test_index_turn_history_batch_uploads_two_separate_files():
    runtime = _make_runtime()
    config = GeminiChatConfig(history_vector_store_id="vs_existing")
    turns = [
        _turn(
            1,
            {"role": "user", "parts": [{"text": "What is OAuth?"}]},
            {
                "role": "model",
                "parts": [{"function_call": {"name": "web_search", "args": {"query": "OAuth"}}}],
            },
        )
    ]

    fake_file = type("FakeFile", (), {"id": "file_abc"})()
    mock_openai_conn = AsyncMock()
    mock_openai_conn.client = object()

    with (
        patch(
            "src.autobots.conn.gemini.gemini_chat.AppConn.get_openai",
            new_callable=AsyncMock,
            return_value=mock_openai_conn,
        ),
        patch(
            "src.autobots.conn.gemini.gemini_chat.OpenaiFiles.create",
            new_callable=AsyncMock,
            return_value=fake_file,
        ),
        patch(
            "src.autobots.conn.gemini.gemini_chat.openai_vector_store_tool",
            new_callable=AsyncMock,
            return_value=_vs_datablocks({"ok": True, "files": []}),
        ) as mock_vs_tool,
    ):
        await runtime._index_turn_history_batch_to_vector_store(
            ctx=Context(), config=config, notify=None, turns=turns, turn_start=1, turn_end=1
        )

    add_files_call = mock_vs_tool.await_args_list[-1]
    files = add_files_call.kwargs["config"].files
    assert len(files) == 2
    assert {f.attributes["content_type"] for f in files} == {"messages", "tool_activity"}


@pytest.mark.asyncio
async def test_index_turn_history_batch_skips_empty_tool_activity_file():
    runtime = _make_runtime()
    config = GeminiChatConfig(history_vector_store_id="vs_existing")
    turns = [_turn(1, {"role": "user", "parts": [{"text": "What is OAuth?"}]})]

    fake_file = type("FakeFile", (), {"id": "file_abc"})()
    mock_openai_conn = AsyncMock()
    mock_openai_conn.client = object()

    with (
        patch(
            "src.autobots.conn.gemini.gemini_chat.AppConn.get_openai",
            new_callable=AsyncMock,
            return_value=mock_openai_conn,
        ),
        patch(
            "src.autobots.conn.gemini.gemini_chat.OpenaiFiles.create",
            new_callable=AsyncMock,
            return_value=fake_file,
        ),
        patch(
            "src.autobots.conn.gemini.gemini_chat.openai_vector_store_tool",
            new_callable=AsyncMock,
            return_value=_vs_datablocks({"ok": True, "files": []}),
        ) as mock_vs_tool,
    ):
        await runtime._index_turn_history_batch_to_vector_store(
            ctx=Context(), config=config, notify=None, turns=turns, turn_start=1, turn_end=1
        )

    add_files_call = mock_vs_tool.await_args_list[-1]
    files = add_files_call.kwargs["config"].files
    assert len(files) == 1
    assert files[0].attributes["content_type"] == "messages"

@pytest.mark.asyncio
async def test_replace_does_nothing_when_no_files_listed():
    runtime = _make_runtime()
    config = GeminiChatConfig(history_vector_store_id="vs_existing")

    with patch(
        "src.autobots.conn.gemini.gemini_chat.openai_vector_store_tool",
        new_callable=AsyncMock,
        return_value=_vs_datablocks({"ok": True, "files": []}),
    ) as mock_vs_tool:
        await runtime._replace_existing_history_vector_store_files(
            Context(), None, config=config, turn_start=1, turn_end=20, content_types={"messages"}
        )

    mock_vs_tool.assert_awaited_once()
    assert mock_vs_tool.await_args.kwargs["config"].operation == OpenAIVectorStoreToolOperation.LIST_VECTOR_STORE_FILES


@pytest.mark.asyncio
async def test_replace_deletes_only_files_matching_exact_range_and_type():
    runtime = _make_runtime()
    config = GeminiChatConfig(history_vector_store_id="vs_existing")
    listed = {
        "ok": True,
        "files": [
            {"id": "file_match", "attributes": {"turn_start": 1, "turn_end": 20, "content_type": "messages"}},
            {"id": "file_other_type", "attributes": {"turn_start": 1, "turn_end": 20, "content_type": "tool_activity"}},
            {"id": "file_other_range", "attributes": {"turn_start": 21, "turn_end": 40, "content_type": "messages"}},
        ],
    }

    with patch(
        "src.autobots.conn.gemini.gemini_chat.openai_vector_store_tool",
        new_callable=AsyncMock,
        side_effect=[_vs_datablocks(listed), _vs_datablocks({"ok": True})],
    ) as mock_vs_tool:
        await runtime._replace_existing_history_vector_store_files(
            Context(), None, config=config, turn_start=1, turn_end=20, content_types={"messages"}
        )

    assert mock_vs_tool.await_count == 2
    delete_call = mock_vs_tool.await_args_list[1]
    delete_config = delete_call.kwargs["config"]
    assert delete_config.operation == OpenAIVectorStoreToolOperation.DELETE_VECTOR_STORE_FILES
    assert delete_config.delete_file_ids == ["file_match"]

@pytest.mark.asyncio
async def test_maybe_index_skips_when_disabled():
    runtime = _make_runtime()
    runtime._ensure_history_vector_store_exists = AsyncMock()
    runtime._index_turn_history_batch_to_vector_store = AsyncMock()
    config = GeminiChatConfig(history_vector_store_enabled=False)

    await runtime._maybe_index_turn_history_to_vector_store(
        ctx=Context(), config=config, notify=None, current_turn=20
    )

    runtime._ensure_history_vector_store_exists.assert_not_called()
    runtime._index_turn_history_batch_to_vector_store.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_index_skips_when_turn_not_a_multiple_of_n():
    runtime = _make_runtime()
    runtime._ensure_history_vector_store_exists = AsyncMock()
    runtime._index_turn_history_batch_to_vector_store = AsyncMock()
    config = GeminiChatConfig(history_vector_store_index_every_n_turns=20)

    await runtime._maybe_index_turn_history_to_vector_store(
        ctx=Context(), config=config, notify=None, current_turn=19
    )

    runtime._ensure_history_vector_store_exists.assert_not_called()
    runtime._index_turn_history_batch_to_vector_store.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_index_skips_when_already_indexed_up_to_that_turn():
    runtime = _make_runtime()
    runtime._ensure_history_vector_store_exists = AsyncMock()
    runtime._index_turn_history_batch_to_vector_store = AsyncMock()
    config = GeminiChatConfig(
        history_vector_store_index_every_n_turns=20,
        history_vector_store_last_indexed_turn=20,
    )

    await runtime._maybe_index_turn_history_to_vector_store(
        ctx=Context(), config=config, notify=None, current_turn=20
    )

    runtime._ensure_history_vector_store_exists.assert_not_called()
    runtime._index_turn_history_batch_to_vector_store.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_index_triggers_on_20th_turn_and_advances_watermark():
    runtime = _make_runtime()
    runtime._ensure_history_vector_store_exists = AsyncMock()
    runtime._index_turn_history_batch_to_vector_store = AsyncMock()

    turn_history = [
        _turn(n, {"role": "user", "parts": [{"text": f"message {n}"}]}) for n in range(1, 21)
    ]
    config = GeminiChatConfig(
        history_vector_store_index_every_n_turns=20,
        history_vector_store_last_indexed_turn=0,
        turn_history=turn_history,
    )

    await runtime._maybe_index_turn_history_to_vector_store(
        ctx=Context(), config=config, notify=None, current_turn=20
    )

    runtime._ensure_history_vector_store_exists.assert_awaited_once()
    runtime._index_turn_history_batch_to_vector_store.assert_awaited_once()

    _, kwargs = runtime._index_turn_history_batch_to_vector_store.await_args
    assert kwargs["turn_start"] == 1
    assert kwargs["turn_end"] == 20
    assert len(kwargs["turns"]) == 20

    assert config.history_vector_store_last_indexed_turn == 20


@pytest.mark.asyncio
async def test_maybe_index_only_batches_turns_since_last_indexed():
    runtime = _make_runtime()
    runtime._ensure_history_vector_store_exists = AsyncMock()
    runtime._index_turn_history_batch_to_vector_store = AsyncMock()

    turn_history = [
        _turn(n, {"role": "user", "parts": [{"text": f"message {n}"}]}) for n in range(1, 41)
    ]
    config = GeminiChatConfig(
        history_vector_store_index_every_n_turns=20,
        history_vector_store_last_indexed_turn=20,
        turn_history=turn_history,
        history_vector_store_id="vs_existing",
    )

    await runtime._maybe_index_turn_history_to_vector_store(
        ctx=Context(), config=config, notify=None, current_turn=40
    )

    _, kwargs = runtime._index_turn_history_batch_to_vector_store.await_args
    assert kwargs["turn_start"] == 21
    assert kwargs["turn_end"] == 40
    assert len(kwargs["turns"]) == 20
    assert all(21 <= t["turn"] <= 40 for t in kwargs["turns"])

    assert config.history_vector_store_last_indexed_turn == 40


@pytest.mark.asyncio
async def test_maybe_index_is_best_effort_and_never_raises():
    """Indexing failures must never break the chat turn itself."""
    runtime = _make_runtime()
    runtime._ensure_history_vector_store_exists = AsyncMock()
    runtime._index_turn_history_batch_to_vector_store = AsyncMock(
        side_effect=RuntimeError("openai is down")
    )

    turn_history = [
        _turn(n, {"role": "user", "parts": [{"text": f"message {n}"}]}) for n in range(1, 21)
    ]
    config = GeminiChatConfig(
        history_vector_store_index_every_n_turns=20,
        history_vector_store_last_indexed_turn=0,
        turn_history=turn_history,
    )

    # Must not raise.
    await runtime._maybe_index_turn_history_to_vector_store(
        ctx=Context(), config=config, notify=None, current_turn=20
    )

    # Watermark must NOT advance on failure, so the batch is retried next time.
    assert config.history_vector_store_last_indexed_turn == 0
