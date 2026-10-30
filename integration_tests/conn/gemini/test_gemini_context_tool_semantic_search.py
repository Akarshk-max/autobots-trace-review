import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.conn.gemini.chat_model_gemini import GeminiChatConfig
from src.autobots.conn.gemini.context_tool.context_tool_models import GeminiContextQueryReq
from src.autobots.conn.gemini.gemini_chat import GeminiChatRuntime
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


def _make_runtime() -> GeminiChatRuntime:
    user = UserORM(id="test_user_id", email="test_user@runkai.ai")
    return GeminiChatRuntime(user=user, db=None, gemini_client=MagicMock())


def _function_call(*, name: str, args: dict, call_id: str = "call_1") -> dict:
    return {"name": name, "args": args, "id": call_id}


def test_gemini_context_query_req_has_semantic_fields_with_sane_defaults():
    req = GeminiContextQueryReq()
    assert req.semantic_query is None
    assert req.semantic_max_results == 5


def test_gemini_context_query_req_semantic_max_results_is_bounded():
    with pytest.raises(ValidationError):
        GeminiContextQueryReq(semantic_max_results=21)
    with pytest.raises(ValidationError):
        GeminiContextQueryReq(semantic_max_results=0)

@pytest.mark.asyncio
async def test_semantic_query_without_vector_store_returns_note_and_skips_search():
    runtime = _make_runtime()
    runtime._last_config = GeminiChatConfig(history_vector_store_id=None)

    with patch(
        "src.autobots.conn.gemini.gemini_chat.openai_vector_store_tool",
        new_callable=AsyncMock,
    ) as mock_vs_tool:
        response_parts, persisted_results, _generated_files = await runtime.run_tool_calls(
            ctx=Context(),
            app_state=AppState(),
            function_calls=[
                _function_call(
                    name="gemini_context_tool",
                    args={"semantic_query": "what did we decide about the OAuth redirect URL?"},
                )
            ],
            notify=None,
            output_text_to_file_char_limit=None,
            step_index=1,
        )

    mock_vs_tool.assert_not_called()
    assert len(persisted_results) == 1
    payload = json.loads(persisted_results[0]["output_text"])
    assert payload["total_count"] == 0
    assert payload["matches"] == []
    assert "not indexed" in payload["meta"]["note"].lower() or "no conversation-memory" in payload["meta"][
        "note"
    ].lower()

@pytest.mark.asyncio
async def test_semantic_query_with_vector_store_queries_openai_vector_store():
    runtime = _make_runtime()
    runtime._last_config = GeminiChatConfig(history_vector_store_id="vs_abc123")

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
        "src.autobots.conn.gemini.gemini_chat.openai_vector_store_tool",
        new_callable=AsyncMock,
        return_value=fake_vs_response,
    ) as mock_vs_tool:
        response_parts, persisted_results, _generated_files = await runtime.run_tool_calls(
            ctx=Context(),
            app_state=AppState(),
            function_calls=[
                _function_call(
                    name="gemini_context_tool",
                    args={
                        "semantic_query": "OAuth redirect URL decision",
                        "semantic_max_results": 3,
                    },
                )
            ],
            notify=None,
            output_text_to_file_char_limit=None,
            step_index=1,
        )

    mock_vs_tool.assert_awaited_once()
    _, kwargs = mock_vs_tool.await_args
    config = kwargs["config"]
    assert config.vector_store_id == "vs_abc123"
    assert config.query == "OAuth redirect URL decision"
    assert config.max_num_results == 3

    assert len(persisted_results) == 1
    payload = json.loads(persisted_results[0]["output_text"])
    assert payload["total_count"] == 1
    assert payload["meta"]["mode"] == "semantic"
    assert "redirect URL is /callback" in payload["matches"][0]["content"][0]
    assert persisted_results[0]["is_error"] is False
