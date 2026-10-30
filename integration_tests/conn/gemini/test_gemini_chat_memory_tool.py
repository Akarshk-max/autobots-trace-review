import asyncio

from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_gemini import (
    ActionText2TextLlmChatGemini,
    GeminiChatActionConfig,
)
from src.autobots.conn.gemini.chat_memory_tool import (
    GeminiChatMemoryToolReq,
    GeminiChatMemoryToolService,
)
from src.autobots.conn.gemini.chat_model_gemini import GeminiChatConfig
from src.autobots.conn.gemini.gemini_chat import GeminiChatRuntime


def test_chat_memory_service_put_get_list_delete_roundtrip():
    memory = []

    put_req = GeminiChatMemoryToolReq(
        action="put",
        kind="decision",
        key="history_replay_policy",
        value="Do not replay old function_response parts across turns.",
        importance="high",
        tags=["history", "gemini", "history"],
    )
    put_res, memory = GeminiChatMemoryToolService.execute(
        put_req,
        chat_memory=memory,
        current_turn=3,
    )
    assert put_res.status == "ok"
    assert put_res.item is not None
    assert put_res.item.tags == ["history", "gemini"]
    assert len(memory) == 1

    update_req = GeminiChatMemoryToolReq(
        action="put",
        kind="decision",
        key="history_replay_policy",
        value="Replay only conversational parts for old turns.",
        importance="high",
        tags=["history", "replay"],
    )
    update_res, memory = GeminiChatMemoryToolService.execute(
        update_req,
        chat_memory=memory,
        current_turn=4,
    )
    assert update_res.item is not None
    assert update_res.item.value == "Replay only conversational parts for old turns."
    assert len(memory) == 1

    get_res, memory = GeminiChatMemoryToolService.execute(
        GeminiChatMemoryToolReq(action="get", key="history_replay_policy"),
        chat_memory=memory,
        current_turn=4,
    )
    assert get_res.status == "ok"
    assert get_res.item is not None
    assert get_res.item.key == "history_replay_policy"

    list_res, memory = GeminiChatMemoryToolService.execute(
        GeminiChatMemoryToolReq(action="list", kind="decision", query="replay"),
        chat_memory=memory,
        current_turn=4,
    )
    assert list_res.count == 1
    assert list_res.items[0].key == "history_replay_policy"

    delete_res, memory = GeminiChatMemoryToolService.execute(
        GeminiChatMemoryToolReq(action="delete", key="history_replay_policy"),
        chat_memory=memory,
        current_turn=5,
    )
    assert delete_res.status == "ok"
    assert memory == []


def test_default_tool_selection_includes_chat_memory_tool():
    config = GeminiChatActionConfig(tool_selection={})

    ActionText2TextLlmChatGemini._ensure_default_tool_selection(config)

    assert "gemini_chat_memory_tool" in config.tool_selection.default_tool_names


def test_run_tool_calls_updates_chat_memory_in_config():
    runtime = GeminiChatRuntime(user=None, db=None, gemini_client=None)
    config = GeminiChatConfig(chat_turn=2)
    runtime._last_config = config

    async def run_once():
        response_parts, persisted_results, generated_files = await runtime.run_tool_calls(
            ctx=None,
            function_calls=[
                {
                    "id": "mem-1",
                    "name": "gemini_chat_memory_tool",
                    "args": {
                        "action": "put",
                        "kind": "goal",
                        "key": "current_goal",
                        "value": "Build Gemini chat memory tool.",
                        "importance": "high",
                        "tags": ["memory", "gemini"],
                    },
                }
            ],
            notify=None,
            output_text_to_file_char_limit=None,
            step_index=1,
        )
        return response_parts, persisted_results, generated_files

    response_parts, persisted_results, generated_files = asyncio.run(run_once())

    assert generated_files == []
    assert len(response_parts) == 1
    assert response_parts[0]["function_response"]["name"] == "gemini_chat_memory_tool"
    assert len(persisted_results) == 1
    assert config.chat_memory[0]["key"] == "current_goal"
    assert config.chat_memory[0]["kind"] == "goal"
