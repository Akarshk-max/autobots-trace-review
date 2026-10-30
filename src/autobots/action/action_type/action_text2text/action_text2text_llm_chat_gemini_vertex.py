from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_gemini import (
    ActionText2TextLlmChatGemini,
    GeminiChatActionConfig,
    GeminiProvider,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.gemini.chat_model_gemini import GeminiModel
from src.autobots.conn.gemini.model_registry_gemini import GeminiProvider as GeminiModelProvider


class GeminiVertexChatConfig(GeminiChatActionConfig):
    model: GeminiModel = "gemini-2.5-flash"
    provider: GeminiModelProvider = GeminiModelProvider.VERTEX
    gemini_provider: str = GeminiProvider.VERTEX


class ActionText2TextLlmChatGeminiVertex(ActionText2TextLlmChatGemini):
    """Gemini chat via the user's own Vertex AI account (BYOK).

    Identical to text2text_llm_chat_gemini but routes through the user's
    Vertex AI credentials ('vertex_ai' app auth: project_id, region, service_account_json).
    """

    type = ActionType.text2text_llm_chat_gemini_vertex

    @staticmethod
    def get_config_create_type():
        return GeminiVertexChatConfig

    @staticmethod
    def get_config_update_type():
        return GeminiVertexChatConfig

    @staticmethod
    def get_config_type():
        return GeminiVertexChatConfig
