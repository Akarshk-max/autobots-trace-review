
from src.autobots.llm.tools.openrouter_inspect_tool import (
    OpenRouterInspectReq,
    execute_openrouter_inspect,
)


def test_openrouter_inspect_chat_history_basic():
    turn_history = [
        {
            "turn": 1,
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        },
        {
            "turn": 2,
            "messages": [
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": "answer"},
            ],
        },
    ]

    pinned_messages = [{"role": "system", "content": "pinned"}]

    req = OpenRouterInspectReq(op="chat_history", include_pinned=True, limit=10)
    res = execute_openrouter_inspect(
        req=req,
        turn_history=turn_history,
        pinned_messages=pinned_messages,
        tool_results=[],
    )

    assert res.total_count == 2
    assert res.returned_count == 3
    assert isinstance(res.results, list)

    # Pinned injected as turn=0 at the top
    assert res.results[0]["turn"] == 0
    assert res.results[0]["pinned"] is True


def test_openrouter_inspect_tool_results_filters():
    tool_results = [
        {"tool_call_id": "1", "name": "daytona_sandbox", "chat_turn": 1, "step_index": 0, "is_error": False},
        {"tool_call_id": "2", "name": "openai_vector_store_tool", "chat_turn": 1, "step_index": 1, "is_error": True},
        {"tool_call_id": "3", "name": "daytona_sandbox", "chat_turn": 2, "step_index": 0, "is_error": False},
    ]

    req = OpenRouterInspectReq(
        op="tool_results",
        tool_names=["daytona_sandbox"],
        chat_turn=1,
        limit=10,
    )

    res = execute_openrouter_inspect(
        req=req,
        turn_history=[],
        pinned_messages=[],
        tool_results=tool_results,
    )

    assert res.total_count == 1
    assert res.returned_count == 1
    assert res.results[0]["tool_call_id"] == "1"
