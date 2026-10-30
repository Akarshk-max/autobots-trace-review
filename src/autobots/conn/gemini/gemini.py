import json

import google.oauth2.service_account

from src.autobots.conn.gemini.gemini_client import GeminiClient, get_gemini_client
from src.autobots.conn.gemini.model_registry_gemini import GeminiProvider


def get_gemini_conn(ctx, api_key: str) -> GeminiClient:
    return get_gemini_client(api_key=api_key)


def get_conn_vertex_gemini(project_id: str, region: str, service_account_json: str) -> GeminiClient:
    creds = google.oauth2.service_account.Credentials.from_service_account_info(
        json.loads(service_account_json),
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    return get_gemini_client(
        provider=GeminiProvider.VERTEX,
        project=project_id,
        location=region,
        credentials=creds,
        api_version=None,  # Vertex AI uses its own default (v1beta1); "v1beta" is Gemini API-only
    )
