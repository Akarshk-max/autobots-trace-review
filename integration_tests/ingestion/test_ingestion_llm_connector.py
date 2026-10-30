# tests/ingestion/test_ingestion_llm_connector.py

import base64
import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from google.genai import types

from src.autobots.conn.gemini.gemini_model import ChatReqGemini
from src.autobots.conn.openai.openai_chat.chat_model import ChatReq
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.ingestion.ingestion_llm_connector import IngestionLLMConnector
from src.autobots.user.user_orm_model import UserORM

pytestmark = pytest.mark.asyncio


# --- Fixtures ---
@pytest_asyncio.fixture
def test_user():
    return UserORM(id="test-user-for-connector")


@pytest_asyncio.fixture
def mock_db():
    return AsyncMock()


@pytest_asyncio.fixture
def mock_ctx():
    return Context()


# --- Unit Tests for Toggle Logic ---


@pytest.mark.parametrize(
    "provider_method_name, global_getter_path, app_conn_path",
    [
        (
            "get_openai_client",
            "src.autobots.ingestion.ingestion_llm_connector.get_openai",
            "src.autobots.ingestion.ingestion_llm_connector.AppConn.get_openai",
        ),
        (
            "get_gemini_client",
            "src.autobots.ingestion.ingestion_llm_connector.get_gemini_conn",
            "src.autobots.ingestion.ingestion_llm_connector.AppConn.get_gemini",
        ),
        (
            "get_claude_client",
            "src.autobots.ingestion.ingestion_llm_connector.get_claude_conn",
            "src.autobots.ingestion.ingestion_llm_connector.AppConn.get_claude",
        ),
    ],
)
async def test_connector_with_toggle_true_uses_global_path(
    provider_method_name,
    global_getter_path,
    app_conn_path,
    test_user,
    mock_db,
    mock_ctx,
    monkeypatch,
):
    """Verifies that when the toggle is TRUE, the connector calls the global getter path."""
    monkeypatch.setattr(
        "src.autobots.ingestion.ingestion_llm_connector.IngestionLLMConnector._FORCE_SYSTEM_KEYS_FOR_INGESTION",
        True,
    )

    with (
        patch(global_getter_path, return_value="global_client") as mock_global_getter,
        patch(
            app_conn_path, new_callable=AsyncMock, return_value="appconn_client"
        ) as mock_app_conn_getter,
    ):
        connector_method = getattr(IngestionLLMConnector, provider_method_name)

        if "openai" in provider_method_name:
            client = await connector_method(
                ctx=mock_ctx, user=test_user, db=mock_db, provider="azure"
            )
        else:
            client = await connector_method(ctx=mock_ctx, user=test_user, db=mock_db)

        assert client == "global_client"
        mock_global_getter.assert_called_once()
        mock_app_conn_getter.assert_not_called()


@pytest.mark.parametrize(
    "provider_method_name, global_getter_path, app_conn_path",
    [
        (
            "get_openai_client",
            "src.autobots.ingestion.ingestion_llm_connector.get_openai",
            "src.autobots.ingestion.ingestion_llm_connector.AppConn.get_openai",
        ),
        (
            "get_gemini_client",
            "src.autobots.ingestion.ingestion_llm_connector.get_gemini_conn",
            "src.autobots.ingestion.ingestion_llm_connector.AppConn.get_gemini",
        ),
        (
            "get_claude_client",
            "src.autobots.ingestion.ingestion_llm_connector.get_claude_conn",
            "src.autobots.ingestion.ingestion_llm_connector.AppConn.get_claude",
        ),
    ],
)
async def test_connector_with_toggle_false_uses_appconn_path(
    provider_method_name,
    global_getter_path,
    app_conn_path,
    test_user,
    mock_db,
    mock_ctx,
    monkeypatch,
):
    """Verifies that when the toggle is FALSE, the connector calls the AppConn path."""
    monkeypatch.setattr(
        "src.autobots.ingestion.ingestion_llm_connector.IngestionLLMConnector._FORCE_SYSTEM_KEYS_FOR_INGESTION",
        False,
    )

    with (
        patch(global_getter_path, return_value="global_client") as mock_global_getter,
        patch(
            app_conn_path, new_callable=AsyncMock, return_value="appconn_client"
        ) as mock_app_conn_getter,
    ):
        connector_method = getattr(IngestionLLMConnector, provider_method_name)

        if "openai" in provider_method_name:
            client = await connector_method(
                ctx=mock_ctx, user=test_user, db=mock_db, provider="azure"
            )
        else:
            client = await connector_method(ctx=mock_ctx, user=test_user, db=mock_db)

        assert client == "appconn_client"
        mock_app_conn_getter.assert_awaited_once()
        mock_global_getter.assert_not_called()


# --- Integration Test with Live API Calls ---


@pytest.mark.skipif(
    not all(
        [
            SettingsProvider.sget().OPENAI_API_KEY,
            SettingsProvider.sget().GEMINI_API_KEY,
            SettingsProvider.sget().CLAUDE_API_KEY,
        ]
    ),
    reason="This integration test requires live API keys for OpenAI, Gemini, and Claude.",
)
@pytest.mark.asyncio
async def test_connector_returns_functional_clients_via_global_path(
    test_user, mock_db, mock_ctx, monkeypatch, set_test_settings
):
    """
    Verifies that when the toggle is TRUE, the connector returns functional clients
    that can make real API calls using the global system keys.
    """
    # ARRANGE
    monkeypatch.setattr(
        "src.autobots.ingestion.ingestion_llm_connector.IngestionLLMConnector._FORCE_SYSTEM_KEYS_FOR_INGESTION",
        True,
    )
    # settings = SettingsProvider.sget()
    provider = "openai"  # "azure" if settings.OPENAI_TYPE_IS_AZURE else "openai"

    # ACT: Get the clients using the global path
    openai_client = await IngestionLLMConnector.get_openai_client(
        ctx=mock_ctx, user=test_user, db=mock_db, provider=provider
    )
    gemini_client = await IngestionLLMConnector.get_gemini_client(
        ctx=mock_ctx, user=test_user, db=mock_db
    )

    # --- Make small, real API calls to verify clients are functional ---

    # 1. OpenAI Call (structured JSON, mimicking GraphRAG)
    openai_req = ChatReq(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Return JSON with a 'status' key set to 'ok'."}],
        response_format={"type": "json_object"},
    )
    openai_response = await openai_client.openai_chat.chat(
        ctx=mock_ctx, app_state=AppState(), chat_req=openai_req
    )

    # 2. Gemini Call (media processing, mimicking media_utils)
    # A cat jpeg, base64 encoded. This is more robust than a 1x1 pixel image.
    cat_64_b64 = "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"
    # --- Construct the new request using types from the new SDK ---
    gemini_media_req = ChatReqGemini(
        model="gemini-2.5-flash",
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part(text="Describe this image in one sentence."),
                    types.Part(
                        inline_data=types.Blob(
                            mime_type="image/png", data=base64.b64decode(cat_64_b64)
                        )
                    ),
                ],
            )
        ],
    )

    # --- Update the client method call from generate_text to generate ---
    gemini_media_response = await gemini_client.generate(
        ctx=mock_ctx, user=test_user, chat_req=gemini_media_req, db=mock_db
    )

    # ASSERT
    # OpenAI Assertion
    assert openai_response is not None
    response_json = json.loads(openai_response.choices[0].message.content)
    assert response_json.get("status") == "ok"

    # Gemini Media Assertion
    assert gemini_media_response is not None
    # --- Update assertion to access text from the Part object ---
    response_text = gemini_media_response.parts[0].text
    assert "cat" in response_text.lower()
