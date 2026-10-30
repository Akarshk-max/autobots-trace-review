from typing import Literal, Type

from src.autobots.action.action_type.abc.ActionABC import (
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_responses_openai import (
    ActionText2TextLlmResponsesOpenai,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.openai.openai_responses.openai_responses import LlmResponsesOpenaiConfig


class LlmResponsesAzureOpenAIBYOKConfig(LlmResponsesOpenaiConfig):
    provider: Literal["azure", "openai", "azure_byok"] = "azure_byok"


class ActionText2TextLlmResponsesAzureOpenAI(ActionText2TextLlmResponsesOpenai):
    """Responses API via the user's own Azure OpenAI deployment (BYOK).

    Identical to text2text_llm_responses_openai but routes through the user's Azure
    credentials ('azure_openai' app auth: api_key, endpoint, api_version).
    Set model to the deployment name on your Azure resource.
    """

    type = ActionType.azure_responses

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return LlmResponsesAzureOpenAIBYOKConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return LlmResponsesAzureOpenAIBYOKConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return LlmResponsesAzureOpenAIBYOKConfig
