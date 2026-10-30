import copy
import json

import pytest
from anthropic.types.beta import BetaTextBlockParam

from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_claude import (
    ActionText2TextLlmChatclaude,
    ClaudeChatConfig,
)
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.user.user_orm_model import UserORM


def assert_json_safe(obj):
    json.dumps(obj)

def assert_no_serialization_iterator(obj):
    if type(obj).__name__ == "SerializationIterator":
        pytest.fail("SerializationIterator leaked!")
    if isinstance(obj, dict):
        for v in obj.values():
            assert_no_serialization_iterator(v)
    elif isinstance(obj, list):
        for v in obj:
            assert_no_serialization_iterator(v)

def test_action_config_is_deepcopy_safe():
    user  = UserORM(id = "abc")
    db = get_mongo_db()
    action_config = ClaudeChatConfig(
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"type": "web_search_20260209", "name": "web_search"}],
    )
    action = ActionText2TextLlmChatclaude(user=user , db=db , action_config=action_config)
    action.action_config = ClaudeChatConfig.model_validate(
        action._to_plain(action.action_config)
    )
    # This was the original failure point
    copy.deepcopy(action.action_config)


def test_to_plain_removes_pydantic_objects():
    user  = UserORM(id = "abc")
    db = get_mongo_db()
    action_config = ClaudeChatConfig(
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"type": "web_search_20260209"}],
        mcp_servers=[],
    )
    action = ActionText2TextLlmChatclaude(user=user , db=db , action_config=action_config)
    obj = BetaTextBlockParam(type="text", text="hello")
    result = action._to_plain(obj)
    assert isinstance(result, dict)
    assert result["text"] == "hello"


def test_to_plain_nested_structures():
    user  = UserORM(id = "abc")
    db = get_mongo_db()
    action_config = ClaudeChatConfig(
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"type": "web_search_20260209"}],
        mcp_servers=[],
    )
    action = ActionText2TextLlmChatclaude(user=user , db=db , action_config=action_config)
    nested = {
        "level1": [
            {"level2": [{"type": "text", "text": "hi"}]}
        ]
    }
    result = action._to_plain(nested)
    assert isinstance(result, dict)
    assert isinstance(result["level1"], list)


def test_to_plain_handles_iterators():
    user  = UserORM(id = "abc")
    db = get_mongo_db()
    action_config = ClaudeChatConfig(
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"type": "web_search_20260209"}],
        mcp_servers=[],
    )
    action = ActionText2TextLlmChatclaude(user=user , db=db, action_config=action_config)
    class FakeIterator:
        def __iter__(self):
            yield 1
            yield 2
    obj = {"data": FakeIterator()}
    result = action._to_plain(obj)
    assert result["data"] == [1, 2]


# message senitization test
@pytest.mark.asyncio
async def test_messages_are_plain_after_preprocess():
    user  = UserORM(id = "abc")
    db = get_mongo_db()
    action_config = ClaudeChatConfig(messages=[])
    action = ActionText2TextLlmChatclaude(user=user , db=db , action_config=action_config)
    class DummyInput:
        text = "hello"
        urls = None
        def get_llm_prompt(self, ctx):
            return "hello"
    await action.preprocess_input_messages(
        ctx=None,
        action_input=DummyInput(),
        claude_client=None,
    )
    messages = action.action_config.messages
    assert isinstance(messages, list)
    assert isinstance(messages[0]["content"], list)
    for item in messages[0]["content"]:
        assert isinstance(item, dict)

def test_full_action_config_sanitization():
    user  = UserORM(id = "abc")
    db = get_mongo_db()
    action_config = ClaudeChatConfig(
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"type": "web_search_20260209"}],
        mcp_servers=[],
    )
    action = ActionText2TextLlmChatclaude(user=user , db=db , action_config=action_config)
    clean = action._to_plain(action.action_config)
    validated = ClaudeChatConfig.model_validate(clean)
    copy.deepcopy(validated)  # must not fail


# regression case where bug will hapeen
def test_no_serialization_iterator_leak():
    user  = UserORM(id = "abc")
    db = get_mongo_db()
    action_config = ClaudeChatConfig(
        messages=[{"role": "user", "content": "test"}],
    )
    action = ActionText2TextLlmChatclaude(user=user , db=db , action_config=action_config)
    clean = action._to_plain(action.action_config)
    assert_no_serialization_iterator(clean)

def test_end_to_end_config_safety():
    user  = UserORM(id = "abc")
    db = get_mongo_db()
    
    action_config = ClaudeChatConfig(
        messages=[{"role": "user", "content": "hello"}],
        tools=[
            {"type": "web_search_20260209"},
            {"type": "code_execution_20250825"},
        ],
        mcp_servers=[],
    )
    action = ActionText2TextLlmChatclaude(user=user , db=db , action_config=action_config)
    cleaned = action._to_plain(action.action_config)
    validated = ClaudeChatConfig.model_validate(cleaned)

    # all guarantees - should not fail here otherwise it will prove bug is still persistant
    copy.deepcopy(validated)
    assert_json_safe(cleaned)
    assert_no_serialization_iterator(cleaned)