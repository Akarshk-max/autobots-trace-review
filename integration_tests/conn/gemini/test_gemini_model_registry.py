from src.autobots.conn.gemini.chat_model_gemini import GeminiChatConfig
from src.autobots.conn.gemini.model_registry_gemini import (
    GeminiModelLifecycle,
    GeminiModelRegistry,
)


def test_registry_resolves_gemini_31_pro_preview():
    resolved = GeminiModelRegistry.resolve(model="gemini-3.1-pro-preview")

    assert resolved.capabilities.family == "gemini-3.1"
    assert resolved.capabilities.lifecycle == GeminiModelLifecycle.PREVIEW
    assert resolved.capabilities.supports_thinking is True
    assert resolved.capabilities.output_token_limit == 65_536


def test_registry_resolves_gemini_31_pro_preview_customtools():
    resolved = GeminiModelRegistry.resolve(model="gemini-3.1-pro-preview-customtools")

    assert resolved.capabilities.family == "gemini-3.1"
    assert resolved.capabilities.lifecycle == GeminiModelLifecycle.PREVIEW
    assert resolved.capabilities.supports_builtin_custom_tool_combination is True


# def test_registry_resolves_gemini_3_flash_preview():
#     resolved = GeminiModelRegistry.resolve(model="gemini-3-flash-preview")

#     assert resolved.capabilities.family == "gemini-3"
#     assert resolved.capabilities.lifecycle == GeminiModelLifecycle.PREVIEW
#     assert resolved.capabilities.supports_function_calling is True


# def test_registry_marks_legacy_gemini_3_aliases_deprecated():
#     resolved = GeminiModelRegistry.resolve(model="gemini-3-pro")

#     assert resolved.capabilities.family == "gemini-3"
#     assert resolved.capabilities.lifecycle == GeminiModelLifecycle.DEPRECATED


def test_gemini_31_chat_config_uses_gemini3_defaults():
    config = GeminiChatConfig(model="gemini-3.1-pro-preview")

    assert config.max_output_tokens == 65_536
    assert config.thinking is not None
    assert config.thinking.thinking_level == "medium"
    assert config.thinking.thinking_budget is None
