from typing import List, Literal, Type

from openai.types.chat import ChatCompletionToolParam

from src.autobots.action.action_type.abc.ActionABC import (
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    ActionText2TextLlmChatOpenai,
    LlmChatOpenaiConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.llm.tools.common_default_tools import COMMON_EXTERNAL_TOOLS


class LlmChatAzureOpenAIBYOKConfig(LlmChatOpenaiConfig):
    provider: Literal["azure", "openai", "azure_byok"] = "azure_byok"
    # Only the common external tools: the OpenAI chat defaults additionally carry
    # file_search_tool and coding_assistant, neither of which a BYOK Azure
    # deployment is guaranteed to back.
    tools: List[ChatCompletionToolParam | str] | None = list(COMMON_EXTERNAL_TOOLS)


class ActionText2TextLlmChatAzureOpenAI(ActionText2TextLlmChatOpenai):
    """Chat via the user's own Azure OpenAI deployment (BYOK).

    Identical to text2text_llm_chat_openai but routes through the user's Azure
    credentials ('azure_openai' app auth: api_key, endpoint, api_version).
    Set model to the deployment name on your Azure resource.
    """

    type = ActionType.azure_chat

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return LlmChatAzureOpenAIBYOKConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return LlmChatAzureOpenAIBYOKConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return LlmChatAzureOpenAIBYOKConfig
