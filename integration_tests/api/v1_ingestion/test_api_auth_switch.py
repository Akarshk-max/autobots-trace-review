# tests/api/v1_ingestion/test_api_auth_switch.py

import uuid
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from starlette.requests import Request

# Import the functions we need to test and the objects we need to mock
from src.autobots.api.v1_ingestion.api_knowledge_base import SearchRequest, perform_rag_search
from src.autobots.conn.chroma.chroma import Document
from src.autobots.core.settings import SettingsProvider
from src.autobots.user.user_orm_model import UserORM

settings = SettingsProvider.sget()
pytestmark = pytest.mark.skipif(
    settings.ENV != "local",
    reason="This test relies on local-only dependency injection patterns (_test_user).",
)


# --- Simple dummy DB to satisfy KB CRUD and membership queries in perform_rag_search ---


class DummyCursor:
    async def to_list(self, length=None):
        # For this test, we don't need any KBs; return empty list.
        return []


class DummyCollection:
    def find(self, *args, **kwargs):
        return DummyCursor()


class DummyDB:
    def __getitem__(self, name: str):
        # name will be "KnowledgeBases", "KnowledgeBaseMemberships", etc.
        return DummyCollection()


@pytest_asyncio.fixture
def test_user():
    """Provides a consistent dummy user for tests."""
    return UserORM(id="test-user-for-auth-switch")


@pytest.mark.asyncio
async def test_api_uses_llm_provider_when_toggle_is_true(test_user, monkeypatch, set_test_settings):
    """
    Verifies that the API endpoint correctly calls the IngestionLLMConnector,
    which has its own internal toggle for forcing system keys.
    """
    # --- ARRANGE ---

    # 1. Monkeypatch the provider's internal toggle to True for this test
    monkeypatch.setattr(
        "src.autobots.ingestion.ingestion_llm_connector.IngestionLLMConnector._FORCE_SYSTEM_KEYS_FOR_INGESTION",
        True,
    )

    # 2. Prepare mock return values for the RAG dependencies
    mock_retrieved_chunk = Document(
        page_content="mock content",
        metadata={
            "chunk_id": str(uuid.uuid4()),
            "source": "mock_document.pdf",  # FIX: Add the required 'source' field
        },
    )

    mock_neo4j_client_instance = AsyncMock()
    mock_neo4j_client_instance.query = MagicMock(return_value=[{"nodes": [], "rels": []}])
    mock_llm_response = MagicMock()
    # This creates the nested structure: response.choices[0].message.content
    mock_llm_response.choices[0].message.content = "This is a successful mock LLM response."

    # 3. Patch the dependencies
    with (
        patch(
            # Patch the NEW connector at the location where it is imported and used
            "src.autobots.api.v1_ingestion.api_knowledge_base.IngestionLLMConnector.get_openai_client",
            new_callable=AsyncMock,
        ) as mock_get_openai_client,
        patch(
            "src.autobots.conn.weaviate.weaviate_store.WeaviateStore.create_store",
            # We need to mock create_store because it's called inside the function
            new_callable=AsyncMock,
        ) as mock_create_store,
        patch(
            "src.autobots.ingestion.neo4j_loader.Neo4jManager.get_client",
            new_callable=AsyncMock,
            return_value=mock_neo4j_client_instance,
        ) as mock_get_neo4j_client,
    ):
        # Configure the mock connector to return a mocked client
        mock_openai_client = AsyncMock()
        mock_openai_client.openai_chat.chat = AsyncMock(return_value=mock_llm_response)
        mock_get_openai_client.return_value = mock_openai_client

        # Configure the mock weaviate store that is returned by create_store
        mock_weaviate_store = AsyncMock()
        mock_weaviate_store.similarity_search_with_score.return_value = [
            (mock_retrieved_chunk, 0.9)
        ]
        mock_create_store.return_value = mock_weaviate_store

        # --- ACT ---
        # Use a dummy DB that supports the minimal async interface used by KB code.
        dummy_db = DummyDB()
        dummy_request_body = SearchRequest(query="test", collection_name="test")

        # Create a mock request object with the state.context attribute that the handler expects.
        # Using SimpleNamespace is a clean way to create this nested structure.
        mock_request = MagicMock(spec=Request)
        mock_request.state = SimpleNamespace(context=MagicMock())

        # Call the function with the signature that is active during testing
        result = await perform_rag_search(
            request=mock_request,
            request_body=dummy_request_body,
            user_res=None,  # Pass None for the production dependency
            # _test_user=test_user,                   # Pass the test user via the correct kwarg
            db=dummy_db,
        )

        # --- ASSERT ---
        assert "This is a successful mock LLM response." in result.blocks[0].text

        # Assert that the API endpoint correctly called the new connector.
        # With multi-KB search, the connector may be called more than once
        # (per-KB synthesis + optional fusion), so we only require that it
        # was awaited at least once with the expected arguments.
        assert mock_get_openai_client.await_count >= 1
        mock_get_openai_client.assert_any_await(ANY, test_user, ANY, ANY)

        # Update assertion to check the mocked store's methods
        mock_create_store.assert_awaited_once()
        mock_weaviate_store.similarity_search_with_score.assert_awaited_once()
        mock_weaviate_store.close.assert_awaited_once()

        # Assert that the Neo4j client was fetched twice:
        # 1. Once for the primary RAG traversal inside _get_rag_data.
        # 2. A second time for the graceful fallback logic in perform_rag_search.
        assert mock_get_neo4j_client.await_count == 2
