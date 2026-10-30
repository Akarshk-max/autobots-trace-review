
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openrouter import (
    ActionText2TextLlmChatOpenRouter,
    OpenRouterChatConfig,
)
from src.autobots.user.user_orm_model import UserORM


def _tool_turn(turn_id: int) -> list[dict]:
    return [
        {"role": "user", "content": f"u{turn_id}"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"tc_{turn_id}",
                    "type": "function",
                    "function": {"name": "dummy", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "content": f"tool out t{turn_id}", "tool_call_id": f"tc_{turn_id}"},
        {"role": "assistant", "content": f"a{turn_id}"},
    ]


def test_previous_tool_messages_turns_respected_when_enabled(set_test_settings):
    cfg = OpenRouterChatConfig(
        pinned_messages=[{"role": "system", "content": "sys"}],
        visible_turns=10,
        include_previous_tool_messages=True,
        previous_tool_messages_turns=2,
        turn_history=[
            {"turn": 1, "messages": [{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}]},
            {"turn": 2, "messages": _tool_turn(2)},
            {"turn": 3, "messages": _tool_turn(3)},
            {"turn": 4, "messages": _tool_turn(4)},
        ],
    )

    action = ActionText2TextLlmChatOpenRouter(
        action_config=cfg,
        user=UserORM(id="test_user_id", email="test_user@runkai.ai"),
        db=None,  # not used by message-window logic
    )

    action._rebuild_working_messages_from_history()

    msgs = [m for m in action.action_config.messages if isinstance(m, dict)]
    tool_texts = [m.get("content") for m in msgs if m.get("role") == "tool"]

    # Only the most recent 2 turns (t3, t4) should include tool messages.
    assert "tool out t2" not in tool_texts
    assert "tool out t3" in tool_texts
    assert "tool out t4" in tool_texts


def test_previous_tool_messages_never_included_when_disabled(set_test_settings):
    cfg = OpenRouterChatConfig(
        pinned_messages=[{"role": "system", "content": "sys"}],
        visible_turns=10,
        include_previous_tool_messages=False,
        previous_tool_messages_turns=2,
        turn_history=[
            {"turn": 1, "messages": [{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}]},
            {"turn": 2, "messages": _tool_turn(2)},
            {"turn": 3, "messages": _tool_turn(3)},
            {"turn": 4, "messages": _tool_turn(4)},
        ],
    )

    action = ActionText2TextLlmChatOpenRouter(
        action_config=cfg,
        user=UserORM(id="test_user_id", email="test_user@runkai.ai"),
        db=None,
    )

    action._rebuild_working_messages_from_history()

    msgs = [m for m in action.action_config.messages if isinstance(m, dict)]

    assert all(m.get("role") != "tool" for m in msgs)
    assert all(
        not (m.get("role") == "assistant" and (m.get("tool_calls") or m.get("function_call")))
        for m in msgs
    )
