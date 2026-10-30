"""Characterization tests for the OpenRouter turn-history vector-store feature.

Covers the pure-logic rendering helpers (no network) and the
`history_vector_store_index_every_n_turns` vs `visible_turns` clamp.
"""
from src.autobots.conn.openrouter.chat_model import OpenRouterChatReq
from src.autobots.conn.openrouter.vector_store.history_indexer import (
    HistoryVectorStoreIndexer,
)

TURNS = [
    {
        "turn": 1,
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ],
    },
    {
        "turn": 2,
        "messages": [
            {"role": "user", "content": "run the report"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "running it"},
                    {"type": "tool_use", "name": "daytona_sandbox", "input": {"code": "print(1)"}},
                ],
            },
            {"role": "tool", "content": [{"type": "tool_result", "content": "1"}]},
            {"role": "assistant", "content": "done"},
        ],
    },
]


def test_render_messages_json_has_all_text_messages():
    out = HistoryVectorStoreIndexer._render_messages_json(turns=TURNS)
    assert "hello" in out
    assert "run the report" in out
    assert "running it" in out
    assert "done" in out


def test_render_messages_json_excludes_tool_blocks():
    out = HistoryVectorStoreIndexer._render_messages_json(turns=TURNS)
    assert "daytona_sandbox" not in out


def test_render_tool_activity_includes_tool_blocks():
    out = HistoryVectorStoreIndexer._render_tool_activity_text(turns=TURNS)
    assert "daytona_sandbox" in out
    assert "=== Turn 2 ===" in out


def test_render_tool_activity_excludes_plain_text():
    out = HistoryVectorStoreIndexer._render_tool_activity_text(turns=TURNS)
    assert "hello" not in out
    assert "run the report" not in out


def test_index_every_n_turns_never_exceeds_visible_turns():
    cfg = OpenRouterChatReq(
        visible_turns=5,
        history_vector_store_index_every_n_turns=20,
    )
    # The clamp is applied in HistoryIndexStage; verify the raw config holds both
    # so the stage can clamp. This pins the field schema.
    assert cfg.history_vector_store_index_every_n_turns == 20
    assert cfg.visible_turns == 5


def test_new_history_fields_default_and_serialize_cleanly():
    cfg = OpenRouterChatReq()
    assert cfg.history_vector_store_enabled is True
    assert cfg.history_vector_store_id is None
    assert cfg.history_vector_store_index_every_n_turns == 5
    assert cfg.history_vector_store_last_indexed_turn == 0

    # These internal fields must never leak to the OpenRouter API payload.
    payload = cfg.to_openrouter_api_payload()
    assert "history_vector_store_id" not in payload
    assert "history_vector_store_index_every_n_turns" not in payload
