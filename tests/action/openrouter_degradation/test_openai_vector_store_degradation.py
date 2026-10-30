from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openrouter import (
    ActionText2TextLlmChatOpenRouter,
)
from src.autobots.data_model.app_state import AppState
from src.autobots.llm.tools.openai_vector_store_tool import (
    OpenAIVectorStoreToolConfig,
    OpenAIVectorStoreToolOperation,
    openai_vector_store_tool,
)

def test_remove_tool_by_name_handles_both_shapes():
    tool_defs = [
        {"type": "function", "function": {"name": "openai_vector_store_tool"}},
        {"type": "function", "function": {"name": "daytona_sandbox"}},
        {"name": "openai_vector_store_tool"},
        {"name": "openrouter_inspect"},
    ]
    result = ActionText2TextLlmChatOpenRouter._remove_tool_by_name(
        tool_defs, "openai_vector_store_tool"
    )
    names = [
        t.get("name") or t.get("function", {}).get("name") for t in result
    ]
    assert names == ["daytona_sandbox", "openrouter_inspect"]


def test_remove_tool_by_name_noop_when_absent_and_empty():
    tool_defs = [{"name": "daytona_sandbox"}]
    assert ActionText2TextLlmChatOpenRouter._remove_tool_by_name(tool_defs, "x") == tool_defs
    assert ActionText2TextLlmChatOpenRouter._remove_tool_by_name(None, "x") == []

@pytest.mark.asyncio
async def test_vector_store_tool_returns_exception_when_no_openai_conn(monkeypatch, app_state):
    import src.autobots.llm.tools.openai_vector_store_tool as vs_mod

    monkeypatch.setattr(
        vs_mod.AppConn,
        "get_openai",
        AsyncMock(return_value=HTTPException(status_code=404, detail="no openai")),
    )

    result = await openai_vector_store_tool(
        config=OpenAIVectorStoreToolConfig(
            operation=OpenAIVectorStoreToolOperation.CREATE_VECTOR_STORE,
            name="test-store",
        ),
        ctx=SimpleNamespace(),
        app_state=app_state,
        user=SimpleNamespace(id="u"),
        db=None,
    )

    assert isinstance(result, HTTPException)
    assert result.status_code == 404
