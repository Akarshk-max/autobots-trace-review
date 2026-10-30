"""AUT-1543: chat_history_query_claude semantic search over the conversation
memory vector store.

Exercises AnthropicChat.get_tool_result_block_param_from_tool_use_block for
the chat_history_query_claude tool: the new semantic_query branch (backed by
an OpenAI vector store, mocked here) and a regression check that the original
exact/substring search path still works unchanged. No real network/OpenAI/
Claude calls are made.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.conn.claude.chat_model import ChatReqClaude, ClaudeMessageParam
from src.autobots.conn.claude.claude_chat import AnthropicChat
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.action_chat_history_query_claude import ChatHistoryQueryReq
from src.autobots.user.user_orm_model import UserORM


def _make_chat() -> AnthropicChat:
    return AnthropicChat(anthropic_client=MagicMock())


def _make_tool_use_block(*, name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(id="toolu_1", name=name, input=args, type="tool_use")


def _make_user() -> UserORM:
    return UserORM(id="test_user_id", email="test_user@runkai.ai")


# --- ChatHistoryQueryReq schema ----------------------------------------------


def test_chat_history_query_req_has_semantic_fields_with_sane_defaults():
    req = ChatHistoryQueryReq()
    assert req.semantic_query is None
    assert req.semantic_max_results == 5


def test_chat_history_query_req_semantic_max_results_is_bounded():
    with pytest.raises(ValidationError):
        ChatHistoryQueryReq(semantic_max_results=21)
    with pytest.raises(ValidationError):
        ChatHistoryQueryReq(semantic_max_results=0)


# --- semantic_query branch ---------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_query_without_vector_store_returns_note_and_skips_search():
    chat = _make_chat()
    chat_req = ChatReqClaude(
        messages=[ClaudeMessageParam(role="user", content="hi")],
        history_vector_store_id=None,  # nothing indexed yet
    )
    tool_use_block = _make_tool_use_block(
        name="chat_history_query_claude",
        args={"semantic_query": "what did we decide about the OAuth redirect URL?"},
    )

    with patch(
        "src.autobots.llm.tools.openai_vector_store_tool.openai_vector_store_tool",
        new_callable=AsyncMock,
    ) as mock_vs_tool:
        envelope = await chat.get_tool_result_block_param_from_tool_use_block(
            ctx=Context(),
            app_state=AppState(),
            user=_make_user(),
            tool_use_block=tool_use_block,
            chat_req=chat_req,
            db=None,
            notify=None,
        )

    mock_vs_tool.assert_not_called()

    text = envelope["tool_result_block"]["content"][0]["text"]
    payload = json.loads(text)
    assert payload["total_count"] == 0
    assert payload["results"] == []
    assert "not indexed" in payload["summary"]["note"].lower() or "no conversation-memory" in payload[
        "summary"
    ]["note"].lower()


@pytest.mark.asyncio
async def test_semantic_query_with_vector_store_queries_openai_vector_store():
    chat = _make_chat()
    chat_req = ChatReqClaude(
        messages=[ClaudeMessageParam(role="user", content="hi")],
        history_vector_store_id="vs_abc123",
    )
    tool_use_block = _make_tool_use_block(
        name="chat_history_query_claude",
        args={"semantic_query": "OAuth redirect URL decision", "semantic_max_results": 3},
    )

    fake_vs_response = DataBlocks(
        blocks=[
            DataBlock(
                text=json.dumps(
                    {
                        "ok": True,
                        "results": [
                            {
                                "file_id": "file_1",
                                "score": 0.87,
                                "content": ["We decided the redirect URL is /callback."],
                            }
                        ],
                    }
                )
            )
        ]
    )

    with patch(
        "src.autobots.llm.tools.openai_vector_store_tool.openai_vector_store_tool",
        new_callable=AsyncMock,
        return_value=fake_vs_response,
    ) as mock_vs_tool:
        envelope = await chat.get_tool_result_block_param_from_tool_use_block(
            ctx=Context(),
            app_state=AppState(),
            user=_make_user(),
            tool_use_block=tool_use_block,
            chat_req=chat_req,
            db=None,
            notify=None,
        )

    mock_vs_tool.assert_awaited_once()
    _, kwargs = mock_vs_tool.await_args
    config = kwargs["config"]
    assert config.vector_store_id == "vs_abc123"
    assert config.query == "OAuth redirect URL decision"
    assert config.max_num_results == 3

    text = envelope["tool_result_block"]["content"][0]["text"]
    payload = json.loads(text)
    assert payload["total_count"] == 1
    assert payload["summary"]["mode"] == "semantic"
    assert "redirect URL is /callback" in payload["results"][0]["content"][0]
    assert envelope["tool_result_block"]["is_error"] is False


# --- exact/structured mode regression (no semantic_query) -------------------


@pytest.mark.asyncio
async def test_exact_mode_still_works_when_semantic_query_not_set():
    chat = _make_chat()
    chat_req = ChatReqClaude(
        messages=[ClaudeMessageParam(role="user", content="hi")],
        turn_history=[
            {
                "turn": 3,
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "Let's discuss OAuth"}]},
                ],
            },
            {
                "turn": 4,
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "Unrelated topic"}]},
                ],
            },
        ],
    )
    tool_use_block = _make_tool_use_block(
        name="chat_history_query_claude",
        args={"text_query": "OAuth", "limit": 5},
    )

    with patch(
        "src.autobots.llm.tools.openai_vector_store_tool.openai_vector_store_tool",
        new_callable=AsyncMock,
    ) as mock_vs_tool:
        envelope = await chat.get_tool_result_block_param_from_tool_use_block(
            ctx=Context(),
            app_state=AppState(),
            user=_make_user(),
            tool_use_block=tool_use_block,
            chat_req=chat_req,
            db=None,
            notify=None,
        )

    mock_vs_tool.assert_not_called()

    text = envelope["tool_result_block"]["content"][0]["text"]
    payload = json.loads(text)
    assert payload["total_count"] == 1
    assert payload["results"][0]["turn"] == 3
