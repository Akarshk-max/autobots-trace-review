# tests/conn/weaviate/unit/test_ingestion_worker.py

import json
import uuid
from datetime import datetime
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from src.autobots.ingestion.job_status import JobState
from src.autobots.ingestion.worker import process_single_job
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.asyncio
@patch("src.autobots.ingestion.worker.cleanup_staging_prefix")
@patch("src.autobots.ingestion.worker.get_mongo_db")
@patch("src.autobots.ingestion.worker.IngestionService")
@patch("src.autobots.ingestion.worker.User")
@patch("src.autobots.ingestion.worker.readiness_probe", new_callable=AsyncMock)
async def test_worker_handles_high_priority_job(
    mock_readiness_probe,
    MockUser,
    MockIngestionService,
    mock_get_mongo,
    mock_cleanup_staging_prefix,
):
    """
    Verify the worker correctly processes a job and sets the final status to AVAILABLE.
    """
    # ARRANGE
    # Mock the service and S3 client
    mock_service_instance = AsyncMock()
    # The service method should return a dictionary with chunk_uuids
    mock_service_instance._execute_ingestion_job.return_value = {
        "processed_data": {"chunk_uuids": ["mock-uuid-for-high-prio"]},
        "successful_targets": ["weaviate", "graphrag"],
        "errors": [],
    }
    MockIngestionService.return_value = mock_service_instance

    # Mock the readiness probe to always return True (successful case)
    mock_readiness_probe.return_value = True

    # Mock the User.get call to return a hydrated user object
    mock_hydrated_user = MagicMock(spec=UserORM)
    mock_hydrated_user.model_dump.return_value = {"id": "user-123", "email": "test@runkai.ai"}
    MockUser.get = AsyncMock(return_value=mock_hydrated_user)

    # Configure the database mock, similar to the low-priority test
    mock_db = AsyncMock()
    mock_collection = AsyncMock()
    mock_updated_doc = {
        "_id": "some_job_id",
        "user_id": "user-123",
        "status": "PROCESSING",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    # Configure the .name attribute to prevent RuntimeWarning from mongo_base_crud.py
    mock_collection.name = "IngestionJobs"
    mock_collection.find_one_and_update = AsyncMock(return_value=mock_updated_doc)
    mock_db.__getitem__.return_value = mock_collection
    mock_get_mongo.side_effect = lambda: iter([mock_db])

    # The expected user is now the fully hydrated object, including the email.
    expected_user = UserORM(id="user-123", email="test@runkai.ai")

    job_payload = {
        "job_id": str(uuid.uuid4()),
        "user_id": "user-123",
        "s3_key": "staging/some-uuid/file.pdf",
        "collection_name": "coll1",
        "original_filename": "file.pdf",
        "job_type": "high_priority",
    }
    job_string = json.dumps(job_payload)

    # ACT
    await process_single_job(job_string, "high_prio_queue", mock_db)

    # ASSERT
    # 1. Was the internal execution method called correctly?
    mock_service_instance._execute_ingestion_job.assert_called_once_with(
        s3_key="staging/some-uuid/file.pdf",
        original_filename="file.pdf",
        collection_name="coll1",
        db=ANY,
        targets=["weaviate", "graphrag"],
        user=expected_user,
        ctx=ANY,
        job_data=ANY,
    )

    # 2. The worker updated status to PROCESSING, then to PROCESSED.
    assert mock_collection.find_one_and_update.await_count == 2
    # The 'update' document is the second positional argument (index 1)
    second_call = mock_collection.find_one_and_update.call_args_list[1]
    update_payload = second_call.args[1]["$set"]
    # The new final state for a successful job is AVAILABLE.
    assert update_payload["status"] == JobState.AVAILABLE

    # 4. Was the staging area cleaned up?
    mock_cleanup_staging_prefix.assert_awaited_once_with("staging/some-uuid/file.pdf")


@pytest.mark.asyncio
@patch("src.autobots.ingestion.worker.get_mongo_db")
@patch("src.autobots.ingestion.worker.IngestionService")
@patch("src.autobots.ingestion.worker.cleanup_staging_prefix")
@patch("src.autobots.ingestion.worker.User")
@patch("src.autobots.ingestion.worker.readiness_probe", new_callable=AsyncMock)
async def test_worker_handles_low_priority_job(
    mock_readiness_probe,
    MockUser,
    mock_cleanup_staging_prefix,
    MockIngestionService,
    mock_get_mongo,
):
    """
    Verify low-priority jobs result in a MongoDB update and S3 cleanup.
    """
    # ARRANGE
    mock_service_instance = AsyncMock()
    # Configure the mock to return the expected dictionary shape
    mock_service_instance._execute_ingestion_job.return_value = {
        "processed_data": {"chunk_uuids": ["mock-uuid-for-low-prio"]},
        "successful_targets": ["weaviate"],  # Simulate a partial success
        "errors": ["GraphRAG failed."],
    }
    MockIngestionService.return_value = mock_service_instance

    # Mock the readiness_probe to simulate a real-world partial failure.
    # The worker code probes each requested backend sequentially.
    # - The first call to readiness_probe will be for 'weaviate'. We want it to succeed.
    # - The second call will be for 'graphrag'. We want it to fail to test the
    #   PARTIAL_AVAILABLE status path.
    # `side_effect` provides return values for consecutive await calls.
    mock_readiness_probe.side_effect = [True, False]

    # Mock the User.get call to return a hydrated user object
    mock_hydrated_user = MagicMock(spec=UserORM)
    mock_hydrated_user.model_dump.return_value = {"id": "user-456", "email": "test@example.com"}
    MockUser.get = AsyncMock(return_value=mock_hydrated_user)

    mock_db = AsyncMock()
    # Create a mock for the collection that has an awaitable `update_one` method
    mock_collection = AsyncMock()
    # This mock needs to cover find_one_and_update, which is now used by our custom update_by_id
    # Configure it to return a dictionary that Pydantic can validate.
    mock_updated_doc = {
        "_id": "some_job_id",
        "user_id": "user-456",
        "status": "PROCESSING",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    # Configure the .name attribute for this test as well
    mock_collection.name = "IngestionJobs"
    mock_collection.find_one_and_update = AsyncMock(return_value=mock_updated_doc)
    # Configure the main db mock to return our collection mock when accessed
    mock_db.__getitem__.return_value = mock_collection
    # Make the mock reusable by having it return a new iterator on each call
    # This prevents StopIteration if next() is called more than once.
    mock_get_mongo.side_effect = lambda: iter([mock_db])

    job_payload = {
        "job_id": str(uuid.uuid4()),
        "user_id": "user-456",
        "s3_key": "staging/job-id-123/report.docx",
        "original_source_key": "files/user-456/report.docx",
        "collection_name": "coll2",
        "original_filename": "report.docx",
        "job_type": "low_priority",
        "targets": ["weaviate", "graphrag"],
    }
    job_string = json.dumps(job_payload)

    # ACT
    await process_single_job(job_string, "low_prio_queue", mock_db)

    # ASSERT
    # 1. Internal execution method was still called
    mock_service_instance._execute_ingestion_job.assert_called_once_with(
        s3_key="staging/job-id-123/report.docx",
        original_filename="report.docx",
        collection_name="coll2",
        db=ANY,
        targets=["weaviate", "graphrag"],
        user=ANY,
        ctx=ANY,
        job_data=ANY,
    )

    # 2. MongoDB was updated to a final state (PARTIAL_AVAILABLE in this case).
    assert mock_collection.find_one_and_update.await_count == 2
    # The 'update' document is the second positional argument (index 1)
    second_call = mock_collection.find_one_and_update.call_args_list[1]
    update_payload = second_call.args[1]["$set"]
    assert update_payload["status"] == JobState.PARTIAL_AVAILABLE

    # 3. Staging area was still cleaned up
    mock_cleanup_staging_prefix.assert_awaited_once_with("staging/job-id-123/report.docx")
