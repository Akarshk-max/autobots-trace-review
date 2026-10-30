import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.conn.grok.grok_chat.grok_chat import GrokChat
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.action_chat_history_query_grok import GrokHistoryQueryReq
from src.autobots.user.user_orm_model import UserORM


def _make_chat() -> GrokChat:
    return GrokChat(client=MagicMock())


def _make_user() -> UserORM:
    return UserORM(id="test_user_id", email="test_user@runkai.ai")

def test_grok_history_query_req_has_semantic_fields_with_sane_defaults():
    req = GrokHistoryQueryReq()
    assert req.semantic_query is None
    assert req.semantic_max_results == 5


def test_grok_history_query_req_semantic_max_results_is_bounded():
    with pytest.raises(ValidationError):
        GrokHistoryQueryReq(semantic_max_results=21)
    with pytest.raises(ValidationError):
        GrokHistoryQueryReq(semantic_max_results=0)

@pytest.mark.asyncio
async def test_semantic_query_without_vector_store_returns_note_and_skips_search():
    chat = _make_chat()

    with patch(
        "src.autobots.llm.tools.openai_vector_store_tool.openai_vector_store_tool",
        new_callable=AsyncMock,
    ) as mock_vs_tool:
        envelope_str = await chat._run_chat_history_query_tool(
            ctx=Context(),
            user=_make_user(),
            db=None,
            raw_args={"semantic_query": "what did we decide about the OAuth redirect URL?"},
            notify=None,
            round_no=1,
            chat_state={"history_vector_store_id": None, "turn_history": [], "pinned_messages": []},
        )

    mock_vs_tool.assert_not_called()

    envelope = json.loads(envelope_str)
    assert envelope["ok"] is True
    payload = json.loads(envelope["text"])
    assert payload["total_count"] == 0
    assert payload["results"] == []
    assert "not indexed" in payload["summary"]["note"].lower() or "no conversation-memory" in payload[
        "summary"
    ]["note"].lower()


@pytest.mark.asyncio
async def test_semantic_query_with_vector_store_queries_openai_vector_store():
    chat = _make_chat()

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
        envelope_str = await chat._run_chat_history_query_tool(
            ctx=Context(),
            user=_make_user(),
            db=None,
            raw_args={"semantic_query": "OAuth redirect URL decision", "semantic_max_results": 3},
            notify=None,
            round_no=1,
            chat_state={"history_vector_store_id": "vs_abc123", "turn_history": [], "pinned_messages": []},
        )

    mock_vs_tool.assert_awaited_once()
    _, kwargs = mock_vs_tool.await_args
    config = kwargs["config"]
    assert config.vector_store_id == "vs_abc123"
    assert config.query == "OAuth redirect URL decision"
    assert config.max_num_results == 3

    envelope = json.loads(envelope_str)
    assert envelope["ok"] is True
    payload = json.loads(envelope["text"])
    assert payload["total_count"] == 1
    assert payload["summary"]["mode"] == "semantic"
    assert "redirect URL is /callback" in payload["results"][0]["content"][0]

@pytest.mark.asyncio
async def test_exact_mode_still_works_when_semantic_query_not_set():
    chat = _make_chat()

    turn_history = [
        {
            "turn": 3,
            "messages": [{"role": "user", "content": "Let's discuss OAuth"}],
        },
        {
            "turn": 4,
            "messages": [{"role": "user", "content": "Unrelated topic"}],
        },
    ]

    with patch(
        "src.autobots.llm.tools.openai_vector_store_tool.openai_vector_store_tool",
        new_callable=AsyncMock,
    ) as mock_vs_tool:
        envelope_str = await chat._run_chat_history_query_tool(
            ctx=Context(),
            user=_make_user(),
            db=None,
            raw_args={"text_query": "OAuth", "limit": 5},
            notify=None,
            round_no=1,
            chat_state={"history_vector_store_id": None, "turn_history": turn_history, "pinned_messages": []},
        )

    mock_vs_tool.assert_not_called()

    envelope = json.loads(envelope_str)
    assert envelope["ok"] is True
    payload = json.loads(envelope["text"])
    assert payload["total_count"] == 1
    assert payload["results"][0]["turn"] == 3