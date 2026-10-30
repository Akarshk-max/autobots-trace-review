# tests/ingestion/test_parsing_strategies.py

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.ingestion.exceptions import RetryableIngestionError
from src.autobots.ingestion.service import IngestionService
from src.autobots.user.user_orm_model import UserORM


@pytest.fixture
def test_user_orm():
    """Provides a consistent UserORM object for tests."""
    return UserORM(id="e2e00000-0000-0000-0000-000000000000")


@pytest.fixture
def mock_db_session():
    """Provides a mock database session."""
    return None


@pytest.fixture
def mock_context(test_user_orm):
    """Provides a mock context object."""
    return Context(user_id=str(test_user_orm.id))


@pytest.fixture
def ingestion_service(test_user_orm):
    """Provides an IngestionService instance scoped to the test user."""
    return IngestionService(user_id=str(test_user_orm.id))


@pytest.fixture(autouse=True)
def mock_weaviate_store(monkeypatch):
    """
    Automatically mocks WeaviateStore.create_store for all tests in this module
    to prevent real network calls and ensure test isolation.
    """

    class _DummyStore:
        async def close(self):
            pass

    monkeypatch.setattr(
        "src.autobots.ingestion.service.WeaviateStore.create_store",
        AsyncMock(return_value=_DummyStore()),
    )


@pytest.mark.asyncio
async def test_fast_with_gemini_happy_path(
    ingestion_service, monkeypatch, test_user_orm, mock_db_session, mock_context
):
    """
    Verifies that the `fast_with_gemini` strategy correctly calls Gemini
    for pre-parsing and then Unstructured's `partition_from_content` with a 'fast' strategy.
    """
    # 1. Arrange: Pin the strategy and spy on key functions
    monkeypatch.setattr(
        SettingsProvider.sget(), "INGESTION_WORKER_PARSING_STRATEGY", "fast_with_gemini"
    )

    gemini_call_count = 0
    unstructured_content_call_count = 0
    unstructured_remote_call_count = 0

    async def spy_gemini(*args, **kwargs):
        nonlocal gemini_call_count
        gemini_call_count += 1
        return "This is the OCR text from Gemini."

    async def spy_unstructured_content(file_content, filename, partition_parameters):
        nonlocal unstructured_content_call_count
        unstructured_content_call_count += 1
        assert partition_parameters.strategy == "fast"
        return [{"type": "NarrativeText", "text": "chunk from fast mode", "metadata": {}}]

    async def spy_unstructured_remote(*args, **kwargs):
        nonlocal unstructured_remote_call_count
        unstructured_remote_call_count += 1
        raise AssertionError("Unstructured.partition_from_remote_url was called unexpectedly!")

    # Create a mock client and attach the spies to it
    mock_unstructured_client = MagicMock()
    mock_unstructured_client.partition_from_content = AsyncMock(
        side_effect=spy_unstructured_content
    )
    mock_unstructured_client.partition_from_remote_url = AsyncMock(
        side_effect=spy_unstructured_remote
    )

    # Patch the function where it is imported and used.
    monkeypatch.setattr("src.autobots.ingestion.service.pre_parse_document_with_gemini", spy_gemini)

    # Patch the media_strategy getter to avoid a real Weaviate call
    monkeypatch.setattr(
        ingestion_service, "_get_media_strategy", AsyncMock(return_value="summarize")
    )

    # 2. Act
    result = await ingestion_service._process_document_from_url(
        file_url="https://example.com/doc.pdf",
        original_filename="doc.pdf",
        collection_name="test_collection",
        user=test_user_orm,
        db=mock_db_session,
        ctx=mock_context,
        unstructure_io_client=mock_unstructured_client,
    )

    # 3. Assert
    assert gemini_call_count == 1
    assert unstructured_content_call_count == 1
    assert unstructured_remote_call_count == 0
    assert result is not None and "documents" in result
    assert "chunk from fast mode" in result["documents"][0].page_content


@pytest.mark.asyncio
async def test_fast_with_gemini_fallback_on_failure(
    ingestion_service, monkeypatch, test_user_orm, mock_db_session, mock_context
):
    """
    Verifies that if Gemini pre-parsing fails, the system gracefully falls back
    to the 'auto' strategy using Unstructured's remote URL partitioner.
    """
    # 1. Arrange: Pin strategy, force Gemini to fail, spy on fallback
    monkeypatch.setattr(
        SettingsProvider.sget(), "INGESTION_WORKER_PARSING_STRATEGY", "fast_with_gemini"
    )

    gemini_call_count = 0
    unstructured_remote_call_count = 0

    async def fail_gemini(*args, **kwargs):
        nonlocal gemini_call_count
        gemini_call_count += 1
        raise RetryableIngestionError("Simulated Gemini API failure")

    async def spy_unstructured_content(*args, **kwargs):
        raise AssertionError("Unstructured.partition_from_content was called unexpectedly!")

    async def spy_unstructured_remote(remote_url, partition_parameters):
        nonlocal unstructured_remote_call_count
        unstructured_remote_call_count += 1
        assert partition_parameters.strategy == "auto"
        return [{"type": "NarrativeText", "text": "chunk from auto fallback", "metadata": {}}]

    # Create a mock client and attach the spies to it
    mock_unstructured_client = MagicMock()
    mock_unstructured_client.partition_from_content = AsyncMock(
        side_effect=spy_unstructured_content
    )
    mock_unstructured_client.partition_from_remote_url = AsyncMock(
        side_effect=spy_unstructured_remote
    )

    monkeypatch.setattr(
        "src.autobots.ingestion.service.pre_parse_document_with_gemini", fail_gemini
    )

    monkeypatch.setattr(
        ingestion_service, "_get_media_strategy", AsyncMock(return_value="summarize")
    )

    # 2. Act
    result = await ingestion_service._process_document_from_url(
        file_url="https://example.com/doc.pdf",
        original_filename="doc.pdf",
        collection_name="test_collection",
        user=test_user_orm,
        db=mock_db_session,
        ctx=mock_context,
        unstructure_io_client=mock_unstructured_client,
    )

    # 3. Assert
    assert gemini_call_count == 1, (
        "Gemini pre-parsing should have been attempted once before failing."
    )
    assert unstructured_remote_call_count == 1, (
        "The 'auto' fallback path should have been called exactly once."
    )
    assert result is not None and "documents" in result
    assert "chunk from auto fallback" in result["documents"][0].page_content


@pytest.mark.asyncio
async def test_no_elements_after_all_paths_returns_none(
    ingestion_service, monkeypatch, test_user_orm, mock_db_session, mock_context
):
    """
    Verifies that if both the Gemini path and the auto fallback path produce no
    elements, the function correctly returns None.
    """
    # 1. Arrange
    monkeypatch.setattr(
        SettingsProvider.sget(), "INGESTION_WORKER_PARSING_STRATEGY", "fast_with_gemini"
    )

    async def mock_gemini(*args, **kwargs):
        return "Some text that Unstructured will ignore."

    async def mock_unstructured_empty(*args, **kwargs):
        return []

    # Create a mock client and attach the spies to it
    mock_unstructured_client = MagicMock()
    mock_unstructured_client.partition_from_content = AsyncMock(side_effect=mock_unstructured_empty)
    mock_unstructured_client.partition_from_remote_url = AsyncMock(
        side_effect=mock_unstructured_empty
    )

    monkeypatch.setattr(
        "src.autobots.ingestion.service.pre_parse_document_with_gemini", mock_gemini
    )

    monkeypatch.setattr(
        ingestion_service, "_get_media_strategy", AsyncMock(return_value="summarize")
    )

    # 2. Act
    result = await ingestion_service._process_document_from_url(
        file_url="https://example.com/doc.pdf",
        original_filename="doc.pdf",
        collection_name="test_collection",
        user=test_user_orm,
        db=mock_db_session,
        ctx=mock_context,
        unstructure_io_client=mock_unstructured_client,
    )

    # 3. Assert
    assert result is None


@pytest.mark.asyncio
async def test_fast_path_empty_fallback_to_auto(
    ingestion_service, monkeypatch, test_user_orm, mock_db_session, mock_context
):
    """
    Verifies that if Gemini succeeds but Unstructured 'fast' returns no elements,
    the system correctly falls back to the 'auto' strategy.
    """
    # 1. Arrange: Set up spies and mocks for this specific control flow
    monkeypatch.setattr(
        SettingsProvider.sget(), "INGESTION_WORKER_PARSING_STRATEGY", "fast_with_gemini"
    )

    gemini_call_count = 0
    unstructured_content_call_count = 0
    unstructured_remote_call_count = 0

    async def spy_gemini(*args, **kwargs):
        nonlocal gemini_call_count
        gemini_call_count += 1
        return "Gemini text that Unstructured fast mode will ignore."

    async def mock_unstructured_content_empty(*args, **kwargs):
        nonlocal unstructured_content_call_count
        unstructured_content_call_count += 1
        return []  # Return empty list to trigger fallback

    async def spy_unstructured_remote_fallback(remote_url, partition_parameters):
        nonlocal unstructured_remote_call_count
        unstructured_remote_call_count += 1
        assert partition_parameters.strategy == "auto"
        return [{"type": "NarrativeText", "text": "chunk from auto fallback", "metadata": {}}]

    # Create a mock client and attach the spies to it
    mock_unstructured_client = MagicMock()
    mock_unstructured_client.partition_from_content = AsyncMock(
        side_effect=mock_unstructured_content_empty
    )
    mock_unstructured_client.partition_from_remote_url = AsyncMock(
        side_effect=spy_unstructured_remote_fallback
    )

    monkeypatch.setattr("src.autobots.ingestion.service.pre_parse_document_with_gemini", spy_gemini)
    monkeypatch.setattr(
        ingestion_service, "_get_media_strategy", AsyncMock(return_value="summarize")
    )

    # 2. Act
    result = await ingestion_service._process_document_from_url(
        "http://a.b/c.pdf",
        "c.pdf",
        "test",
        test_user_orm,
        mock_db_session,
        mock_context,
        unstructure_io_client=mock_unstructured_client,
    )

    # 3. Assert
    assert gemini_call_count == 1
    assert unstructured_content_call_count == 1
    assert unstructured_remote_call_count == 1
    assert result is not None and "chunk from auto fallback" in result["documents"][0].page_content
