from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_claude import (
    ActionText2TextLlmChatclaude,
    ClaudeChatConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.claude.model_registry import ClaudeProvider


class ClaudeVertexChatConfig(ClaudeChatConfig):
    provider: ClaudeProvider = ClaudeProvider.VERTEX


class ActionText2TextLlmChatClaudeVertex(ActionText2TextLlmChatclaude):
    """Claude chat via the user's own Vertex AI account (BYOK).

    Identical to text2text_llm_chat_claude but routes through the user's
    Vertex AI credentials ('vertex_ai' app auth: project_id, region, service_account_json).
    """

    type = ActionType.text2text_llm_chat_claude_vertex

    @staticmethod
    def get_config_create_type():
        return ClaudeVertexChatConfig

    @staticmethod
    def get_config_update_type():
        return ClaudeVertexChatConfig

    @staticmethod
    def get_config_type():
        return ClaudeVertexChatConfig
