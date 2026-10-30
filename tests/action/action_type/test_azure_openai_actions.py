import inspect
import typing

from src.autobots.action.action_type.action_llm.action_llm import LlmConfig
from src.autobots.action.action_type.action_map import ACTION_MAP
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_azure_openai import (
    ActionText2TextLlmChatAzureOpenAI,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_responses_azure_openai import (
    ActionText2TextLlmResponsesAzureOpenAI,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.openai.openai_chat.openai_chat import OpenaiChat
from src.autobots.conn.openai.openai_responses.openai_responses import (
    LlmResponsesOpenaiConfig,
    OpenaiResponses,
)


def test_azure_action_types_exist():
    assert ActionType.azure_chat.value == "azure_chat"
    assert ActionType.azure_responses.value == "azure_responses"


def test_action_map_registers_azure_actions():
    assert ACTION_MAP[ActionType.azure_chat] is ActionText2TextLlmChatAzureOpenAI
    assert ACTION_MAP[ActionType.azure_responses] is ActionText2TextLlmResponsesAzureOpenAI


def test_azure_chat_config_defaults_to_azure_byok_provider():
    config_type = ACTION_MAP[ActionType.azure_chat].get_config_type()
    # model is required on the chat request; provider must default on its own.
    config = config_type(model="my-deployment", messages=[])
    assert config.provider == "azure_byok"
    assert ACTION_MAP[ActionType.azure_chat].get_config_create_type() is config_type
    assert ACTION_MAP[ActionType.azure_chat].get_config_update_type() is config_type


def test_azure_responses_config_defaults_to_azure_byok_provider():
    config_type = ACTION_MAP[ActionType.azure_responses].get_config_type()
    # `model` stays constrained to the base RESPONSES_MODELS literal, so an
    # arbitrary Azure deployment name is not accepted here; only provider is
    # what this action overrides.
    config = config_type(input="")
    assert config.provider == "azure_byok"
    assert ACTION_MAP[ActionType.azure_responses].get_config_create_type() is config_type
    assert ACTION_MAP[ActionType.azure_responses].get_config_update_type() is config_type


def test_azure_action_types_are_set_on_the_action_classes():
    assert ActionText2TextLlmChatAzureOpenAI.type == ActionType.azure_chat
    assert ActionText2TextLlmResponsesAzureOpenAI.type == ActionType.azure_responses


def test_llm_surfaces_accept_and_dispatch_on_azure_byok():
    for surface in (OpenaiChat, OpenaiResponses):
        annotation = inspect.signature(surface.__init__).parameters["provider"].annotation
        assert "azure_byok" in typing.get_args(annotation), surface
    assert "azure_byok" in typing.get_args(
        LlmResponsesOpenaiConfig.model_fields["provider"].annotation
    )
    for surface in (OpenaiChat, OpenaiResponses):
        source = inspect.getsource(surface)
        assert 'self.provider in ("openai", "azure_byok")' in source, surface


def test_action_llm_routes_azure_byok_with_app_state():
    assert "azure_byok" in typing.get_args(LlmConfig.model_fields["provider"].annotation)

    from src.autobots.action.action_type.action_llm.action_llm import ActionLlm

    source = inspect.getsource(ActionLlm.run_action)
    assert 'case "azure_byok":' in source
    branch = source.split('case "azure_byok":', 1)[1].split('case "gemini":', 1)[0]
    assert "ActionText2TextLlmChatAzureOpenAI(" in branch
    assert "app_state=app_state" in branch


def test_responses_config_reconstruction_preserves_concrete_subclass():
    from src.autobots.action.action_type.action_text2text.action_text2text_llm_responses_openai import (  # noqa: E501
        ActionText2TextLlmResponsesOpenai,
    )

    source = inspect.getsource(ActionText2TextLlmResponsesOpenai.get_model_based_config)
    assert "type(responses_req)" in source
    assert "LlmResponsesOpenaiConfig(**responses_dict)" not in source
