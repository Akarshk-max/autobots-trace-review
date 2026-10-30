# tests/conn/weaviate/unit/test_ingestion_service.py

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.autobots.conn.chroma.chroma import Document
from src.autobots.conn.neo4j.ontology import ExtractedGraph
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.ingestion.service import IngestionService
from src.autobots.user.user_orm_model import UserORM

pytestmark = pytest.mark.asyncio

# =================================================================================
# TEST 1: Parameterized unit test for the core ingestion job logic
# =================================================================================


@pytest.mark.parametrize(
    "weaviate_succeeds, graphrag_succeeds, expected_successful_targets, expected_error_count, neo4j_should_be_called",
    [
        # SCENARIO 1: Perfect run, both targets succeed.
        (True, True, ["weaviate", "graphrag"], 0, True),
        # SCENARIO 2: Partial success, Weaviate succeeds but GraphRAG fails.
        (True, False, ["weaviate"], 1, True),
        # SCENARIO 3: Critical failure, Weaviate fails. GraphRAG should not even be attempted.
        (False, True, [], 1, False),
    ],
)
@patch("src.autobots.ingestion.service.AppConn.get_unstructured_io", new_callable=AsyncMock)
@patch("src.autobots.ingestion.service.Neo4jLoader", autospec=True)
@patch("src.autobots.ingestion.service.GraphRAGProcessor", autospec=True)
@patch("src.autobots.ingestion.service.WeaviateLoader", autospec=True)
@patch(
    "src.autobots.ingestion.service.IngestionService._process_document_from_url",
    new_callable=AsyncMock,
)
@patch("src.autobots.ingestion.service.WeaviateStore.create_store", new_callable=AsyncMock)
async def test_execute_ingestion_job_scenarios(
    mock_create_store,
    mock_process_document,
    mock_weaviate_loader_class,
    mock_graphrag_processor_class,
    mock_neo4j_loader_class,
    mock_app_conn_get,
    weaviate_succeeds,
    graphrag_succeeds,
    expected_successful_targets,
    expected_error_count,
    neo4j_should_be_called,
):
    """
    Unit tests the orchestration and return value of `_execute_ingestion_job`
    for full success, partial success, and critical failure scenarios.
    """
    # ARRANGE
    # Mock the common dependencies
    mock_store_instance = AsyncMock()
    mock_create_store.return_value = mock_store_instance

    # Mock AppConn to return a dummy client
    mock_unstructured_client = MagicMock()
    mock_app_conn_get.return_value = mock_unstructured_client

    processor_output = {
        "documents": [
            Document(
                page_content="mocked content",
                metadata={"s3_url": "https://fake-s3-url.com/file.pdf", "order": 0},
            )
        ],
        "images_for_vectorization": [],  # FIX: Updated key name to match production code
        "chunk_uuids": ["mock-uuid-123"],
    }
    mock_process_document.return_value = processor_output

    # Configure loader mocks based on the test parameters
    mock_weaviate_loader_instance = mock_weaviate_loader_class.return_value
    mock_weaviate_loader_instance.load.return_value = weaviate_succeeds

    mock_neo4j_loader_instance = mock_neo4j_loader_class.return_value
    mock_neo4j_loader_instance.load.return_value = graphrag_succeeds

    # Configure graph processor mock (only relevant if it gets called)
    mock_graphrag_processor_instance = mock_graphrag_processor_class.return_value
    mock_graphrag_processor_instance.process.return_value = ExtractedGraph()

    service = IngestionService(user_id="test-user-123")
    service.s3_client = AsyncMock()

    # SMART MOCK: Use a function for side_effect to make the mock robust.
    # It returns a URL based on the input key, independent of call order or count.
    def s3_url_side_effect(key):
        if "path/to/document.pdf" in key:
            # This is the staging key from mock_job_data
            return "https://staging-url.com/file.pdf"
        elif "permanent-url" in str(key):  # Fallback check for reconstruction from URL
            return "https://permanent-url.com/file.pdf"
        # Fallback for any other unexpected calls
        return "https://default-mock-url.com/file.pdf"

    service.s3_client.get_https_url.side_effect = s3_url_side_effect

    # Create a realistic mock job payload, which is now required by the method signature.
    mock_job_data = {
        "job_id": "mock-job-id",
        "s3_key": "path/to/document.pdf",
        "persisted_s3_url": "https://permanent-url.com/file.pdf",
        "user_id": "test-user-123",
    }
    mock_user = UserORM(id="test-user-123")
    mock_ctx = Context(user_id="test-user-123")

    # ACT - The function is called for all scenarios and should not raise.
    result = await service._execute_ingestion_job(
        s3_key="path/to/document.pdf",
        original_filename="document.pdf",
        collection_name="test_collection",
        db=AsyncMock(),
        targets=["weaviate", "graphrag"],
        user=mock_user,
        ctx=mock_ctx,
        app_state=AppState(),
        job_data=mock_job_data,
    )

    # Assert on the return value dictionary, which is the core behavior
    assert result["successful_targets"] == expected_successful_targets
    assert len(result["errors"]) == expected_error_count
    assert result["processed_data"] == processor_output

    # Assert that loaders were called (or not called) correctly
    mock_weaviate_loader_instance.load.assert_awaited_once()

    # VERIFY THE FIX: Assert that the processor was called with the correct URLs.
    # The first argument is the staging URL (for processing), and the keyword argument
    # `url_for_metadata` should be the permanent URL.
    # Assert that the processor was called with the correct URLs.
    mock_process_document.assert_awaited_once()
    call_args, call_kwargs = mock_process_document.await_args
    assert call_args[0] == "https://staging-url.com/file.pdf"  # Processing URL
    # call_args[6] corresponds to unstructure_io_client in the updated signature
    assert call_args[6] == mock_unstructured_client
    assert call_kwargs["url_for_metadata"] == "https://permanent-url.com/file.pdf"  # Metadata URL

    if neo4j_should_be_called:
        mock_neo4j_loader_instance.load.assert_awaited_once()
    else:
        mock_neo4j_loader_instance.load.assert_not_called()


# ==========================================================
# TEST 2: High-level test for the file routing logic
# ==========================================================
@patch("src.autobots.ingestion.service.AppConn.get_unstructured_io", new_callable=AsyncMock)
@patch("src.autobots.ingestion.service.get_public_storage")
@patch("src.autobots.ingestion.service.WeaviateStore.create_store", new_callable=AsyncMock)
@patch("src.autobots.ingestion.service.WeaviateLoader", autospec=True)
@patch(
    "src.autobots.ingestion.service.IngestionService._process_document_from_url",
    new_callable=AsyncMock,
)
@patch(
    "src.autobots.ingestion.service.IngestionService._ingest_direct_media_from_url",
    new_callable=AsyncMock,
)
async def test_ingestion_service_file_routing(
    mock_ingest_media,
    mock_process_doc,
    mock_weaviate_loader_class,
    mock_create_store,
    mock_get_public_storage,
    mock_app_conn_get,
):
    """
    Tests that IngestionService calls the correct internal method based on file extension.
    """
    # ARRANGE
    service = IngestionService(user_id="test-user")

    mock_get_public_storage.assert_called_once_with(object_prefix="")
    mock_s3_instance = mock_get_public_storage.return_value

    # Mock AppConn to return a known client mock
    mock_unstructured_client = MagicMock(name="mock_unstructured_client")
    mock_app_conn_get.return_value = mock_unstructured_client

    # Correct side_effect: The only call to get_https_url is for the processing_url.
    # Since persisted_s3_url is provided, the helper function doesn't call it.
    # We expect one call for the PDF test, then one call for the image test.
    mock_s3_instance.get_https_url = AsyncMock(
        side_effect=[
            "http://fake-s3-staging-url/doc.pdf",  # Call 1 for PDF processing
            "http://fake-s3-staging-url/image.png",  # Call 2 for Image processing
        ]
    )

    # Create mock job data for the calls
    mock_job_data = {
        "s3_key": "key",
        "persisted_s3_url": "http://fake-s3-permanent-url/file",
        "user_id": "test-user",
    }

    mock_db = AsyncMock()
    mock_user = UserORM(id="test-user")
    mock_ctx = Context(user_id="test-user")

    valid_processed_data = {
        "documents": [
            Document(
                page_content="test", metadata={"s3_url": "http://fake-s3-url/file", "order": 0}
            )
        ],
        "images_for_vectorization": [],
        "chunk_uuids": [str(uuid.uuid4())],
    }
    mock_process_doc.return_value = valid_processed_data
    mock_ingest_media.return_value = valid_processed_data

    mock_loader_instance = mock_weaviate_loader_class.return_value
    mock_loader_instance.load.return_value = True
    mock_create_store.return_value = AsyncMock()

    # --- TEST CASE 1: PDF Document ---
    await service._execute_ingestion_job(
        "key",
        "mydoc.pdf",
        "coll",
        db=mock_db,
        targets=["weaviate"],
        user=mock_user,
        ctx=mock_ctx,
        app_state=AppState(),
        job_data=mock_job_data,
    )
    # Assert with the correct signature and arguments
    # UPDATED: Added mock_unstructured_client argument
    mock_process_doc.assert_awaited_once_with(
        "http://fake-s3-staging-url/doc.pdf",
        "mydoc.pdf",
        "coll",
        mock_user,
        mock_db,
        mock_ctx,
        mock_unstructured_client,  # New expected arg
        mock_create_store.return_value,
        url_for_metadata="http://fake-s3-permanent-url/file",
    )
    mock_ingest_media.assert_not_called()

    mock_process_doc.reset_mock()
    mock_ingest_media.reset_mock()

    # --- TEST CASE 2: PNG Image ---
    await service._execute_ingestion_job(
        "key",
        "my_image.png",
        "coll",
        db=mock_db,
        targets=["weaviate"],
        user=mock_user,
        ctx=mock_ctx,
        app_state=AppState(),
        job_data=mock_job_data,
    )
    mock_process_doc.assert_not_called()
    # Add a strong assertion for the image case as well
    # UPDATED: Added mock_unstructured_client argument
    mock_ingest_media.assert_awaited_once_with(
        "http://fake-s3-staging-url/image.png",  # 1. file_url
        "my_image.png",  # 2. original_filename
        "coll",  # 3. collection_name
        mock_user,  # 4. user
        mock_db,  # 5. db
        mock_ctx,  # 6. ctx
        mock_unstructured_client,  # 7. New expected arg
        mock_create_store.return_value,  # 8. store
        force_strategy="summarize",  # 9. kwarg
        url_for_metadata="http://fake-s3-permanent-url/file",  # 10. kwarg
    )


# ==========================================================
# TEST 3: Test for the queuing logic
# ==========================================================
@patch("src.autobots.ingestion.service.get_async_redis", new_callable=AsyncMock)
@patch("src.autobots.ingestion.service.JobStatusCRUD")
async def test_queue_ingestion_creates_job_and_queues_to_redis(MockJobStatusCRUD, mock_get_async_redis):
    # ARRANGE
    mock_job_crud_instance = MockJobStatusCRUD.return_value
    mock_job_crud_instance.create = AsyncMock()

    mock_redis_instance = mock_get_async_redis.return_value
    mock_redis_instance.lpush = AsyncMock()
    service = IngestionService(user_id="test-user")

    # ACT
    job_id = await service.queue_ingestion_from_s3(
        "s3_key", "file.txt", "coll", AsyncMock(), ["weaviate"]
    )

    # ASSERT
    mock_job_crud_instance.create.assert_awaited_once()

    # IMPROVED ASSERTION: Check the content of the Redis payload
    mock_redis_instance.lpush.assert_awaited_once()

    # Correctly and cleanly extract the payload string.
    # .call_args.args is the tuple of positional arguments: (queue_name, payload_string)
    job_payload_str = mock_redis_instance.lpush.call_args.args[1]
    job_payload = json.loads(job_payload_str)

    assert job_payload["user_id"] == "test-user"
    assert job_payload["s3_key"] == "s3_key"
    assert job_payload["original_filename"] == "file.txt"
    assert job_payload["collection_name"] == "coll"
    assert job_payload["targets"] == ["weaviate"]
    assert "job_id" in job_payload

    assert isinstance(job_id, str)
