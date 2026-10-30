from src.autobots.conn.gemini.chat_model_gemini import GeminiChatConfig, GeminiThinkingConfig
from src.autobots.conn.gemini.gemini_chat import GeminiChatRuntime


def test_gemini35_defaults_to_medium_thinking_and_model_output_limit():
    config = GeminiChatConfig(model="gemini-3.5-flash")

    assert config.max_output_tokens == 65_536
    assert isinstance(config.thinking, GeminiThinkingConfig)
    assert config.thinking.thinking_level == "medium"
    assert config.thinking.thinking_budget is None
    assert config.use_model_default_max_output_tokens is True


def test_gemini35_ignores_stale_small_max_output_tokens_when_using_model_defaults():
    config = GeminiChatConfig(model="gemini-3.5-flash", max_output_tokens=2048)

    assert config.max_output_tokens == 65_536


def test_gemini35_can_still_respect_explicit_custom_max_output_tokens_when_requested():
    config = GeminiChatConfig(
        model="gemini-3.5-flash",
        max_output_tokens=4096,
        use_model_default_max_output_tokens=False,
    )

    assert config.max_output_tokens == 4096


def test_gemini25_keeps_budget_based_thinking_defaults():
    config = GeminiChatConfig(model="gemini-2.5-flash")

    assert isinstance(config.thinking, GeminiThinkingConfig)
    assert config.thinking.thinking_level is None
    assert config.thinking.thinking_budget == -1


def test_build_generation_config_uses_thinking_level_for_gemini35():
    config = GeminiChatConfig(model="gemini-3.5-flash")

    generation_config = GeminiChatRuntime.build_generation_config(config=config, tool_defs=[])
    payload = generation_config.model_dump(exclude_none=True)
    thinking_cfg = payload["thinking_config"]

    assert str(thinking_cfg["thinking_level"]).lower().endswith("medium")
    assert "thinking_budget" not in thinking_cfg
    assert payload["max_output_tokens"] == 65_536
