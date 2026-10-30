import json
from functools import lru_cache

from anthropic import AsyncAnthropicVertex
from httpx2 import Timeout

from src.autobots.conn.claude.claude_chat import AnthropicChat
from src.autobots.core.settings import Settings, SettingsProvider


class VertexClaude:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        #  organization: str | None = None,
        region: str | None = None,
        project_id: str | None = None,
        timeout: float | Timeout | None = None,
    ):
        self.client = AsyncAnthropicVertex(region=region, project_id=project_id, timeout=timeout)
        self.claude_chat = AnthropicChat(self.client)


@lru_cache
def get_vertex_claude(settings: Settings = SettingsProvider.sget()) -> VertexClaude:
    return VertexClaude(
        api_key=settings.CLAUDE_VERTEX_API_KEY,
        region=settings.CLAUDE_VERTEX_REGION,
        project_id=settings.CLAUDE_VERTEX_PROJECT_ID,
        timeout=3600,
    )


def get_conn_vertex_claude(project_id: str, region: str, service_account_json: str) -> VertexClaude:
    import google.oauth2.service_account

    creds = google.oauth2.service_account.Credentials.from_service_account_info(
        json.loads(service_account_json),
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    vertex = VertexClaude.__new__(VertexClaude)
    vertex.client = AsyncAnthropicVertex(region=region, project_id=project_id, credentials=creds, timeout=3600)
    vertex.claude_chat = AnthropicChat(vertex.client)
    return vertex
