# tests/conn/weaviate/unit/test_nightly_s3_sync.py

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.autobots.core.settings import SettingsProvider
from src.autobots.schedule.schedule import run_nightly_s3_sync


# Mock the UserORM object for type consistency in tests
class MockUser:
    def __init__(self, id):
        self.id = id

    @staticmethod
    def model_validate(data):
        # Handle both "id" and "_id" fields for flexibility, mirroring the application logic.
        user_id = data.get("_id") or data.get("id")
        if not user_id:
            raise ValueError("MockUser requires either 'id' or '_id' field")
        return MockUser(user_id)


@pytest.fixture
def mock_dependencies():
    """A single fixture to set up all common mocks for the tests."""
    with (
        patch("src.autobots.schedule.schedule.JobStatusCRUD") as MockJobStatusCRUD,
        patch("src.autobots.schedule.schedule.get_public_storage") as mock_get_public_storage,
        patch(
            "src.autobots.schedule.schedule.get_async_redis",
            new_callable=AsyncMock,
        ) as mock_get_redis,
        patch("src.autobots.schedule.schedule.get_mongo_db") as mock_get_mongo,
        patch(
            "src.autobots.schedule.schedule.UserORM.model_validate",
            new=MockUser.model_validate,
        ),
    ):
        # --- Configure Mocks ---
        mock_redis_instance = MagicMock()
        mock_redis_instance.lpush = AsyncMock()
        mock_get_redis.return_value = mock_redis_instance

        # Configure the mock for JobStatusCRUD
        mock_job_crud_instance = MagicMock()
        mock_job_crud_instance.create = AsyncMock()
        MockJobStatusCRUD.return_value = mock_job_crud_instance

        mock_storage = AsyncMock()
        mock_storage.provider = "s3"
        mock_storage.container_name = "test-bucket"
        mock_storage.copy = AsyncMock()
        mock_storage.delete_key = AsyncMock()
        mock_storage.get_https_url = AsyncMock(
            side_effect=lambda key: f"https://test-bucket.s3.amazonaws.com/{key}"
        )
        mock_get_public_storage.return_value = mock_storage

        # Create separate mocks for each collection
        mock_users_collection = MagicMock()
        mock_processed_files_collection = MagicMock()

        # This is the main database mock object
        mock_db = MagicMock()

        # Use a side_effect router to return the correct collection mock
        def db_getitem_router(key):
            if key == "Users":
                return mock_users_collection
            elif key == "processed_files_log":
                return mock_processed_files_collection
            return MagicMock()  # Return a default mock for any other key

        mock_db.__getitem__.side_effect = db_getitem_router

        mock_get_mongo.return_value = iter([mock_db])

        yield {
            "get_public_storage": mock_get_public_storage,
            "get_redis": mock_get_redis,
            "get_mongo": mock_get_mongo,
            "redis_instance": mock_redis_instance,
            "storage": mock_storage,
            "db": mock_db,
            "users_collection": mock_users_collection,
            "job_crud_instance": mock_job_crud_instance,
            "processed_files_collection": mock_processed_files_collection,
        }


@pytest.mark.asyncio
async def test_nightly_sync_in_whitelist_mode(mock_dependencies, monkeypatch):
    """
    Tests the sync job when SYNC_MODE is 'WHITELIST'.
    This test verifies that only the users specified in the list are processed.
    """
    # ARRANGE
    # Get the settings singleton to modify its attributes for the test
    settings = SettingsProvider.sget()
    # Force the SYNC_MODE to 'WHITELIST' for this test.
    monkeypatch.setattr(settings, "INGESTION_WORKER_S3_SYNC_MODE", "WHITELIST")
    # Define the exact list of users we expect to be processed for this test run.
    whitelisted_users = ["whitelisted-user-1", "whitelisted-user-2"]
    monkeypatch.setattr(settings, "INGESTION_WORKER_S3_SYNC_WHITELIST_USER_IDS", whitelisted_users)
    # Ensure blacklist is empty for this baseline test
    monkeypatch.setattr(settings, "INGESTION_WORKER_S3_SYNC_BLACKLIST_USER_IDS", [])

    # Get mocks from the fixture
    mock_storage = mock_dependencies["storage"]
    mock_redis_instance = mock_dependencies["redis_instance"]
    mock_processed_files_collection = mock_dependencies["processed_files_collection"]
    mock_job_crud_instance = mock_dependencies["job_crud_instance"]

    # Configure S3 list to return a different file for each user in our test list
    mock_storage.list = AsyncMock(
        side_effect=[
            [MagicMock(key="files/whitelisted-user-1/report.docx")],
            [MagicMock(key="files/whitelisted-user-2/archive.zip")],
        ]
    )

    # Configure collection methods to be awaitable
    mock_processed_files_collection.find_one = AsyncMock(return_value=None)
    mock_processed_files_collection.update_one = AsyncMock()

    async def mock_get_filename(key):
        return key.split("/")[-1]

    mock_storage.get_filename = AsyncMock(side_effect=mock_get_filename)

    # ACT
    await run_nightly_s3_sync()

    # ASSERT
    # Ensure the 'Users' collection was NOT queried, as the user list was hardcoded.
    mock_dependencies["users_collection"].find.assert_not_called()

    # Assert that a job was queued for each user in our test list.
    assert mock_redis_instance.lpush.call_count == len(whitelisted_users)
    assert mock_job_crud_instance.create.call_count == len(whitelisted_users)

    # Check the contents of the queued jobs
    call_payloads = [json.loads(call[0][1]) for call in mock_redis_instance.lpush.call_args_list]
    user_ids_in_payloads = {p["user_id"] for p in call_payloads}
    assert user_ids_in_payloads == set(whitelisted_users)

    assert mock_storage.list.await_count == len(whitelisted_users)
    assert mock_storage.copy.await_count == len(whitelisted_users)
    listed_prefixes = {awaited.kwargs["prefix"] for awaited in mock_storage.list.await_args_list}
    assert listed_prefixes == {
        "files/whitelisted-user-1/",
        "files/whitelisted-user-2/",
    }


@pytest.mark.asyncio
async def test_nightly_sync_all_users_respects_blacklist_in_db_query(
    mock_dependencies, monkeypatch
):
    """
    Tests that in 'ALL_USERS' mode, the MongoDB query is correctly constructed
    to exclude blacklisted users using the '_id' field.
    """
    # ARRANGE
    # Get the settings singleton to modify its attributes for the test
    settings = SettingsProvider.sget()
    # Force the SYNC_MODE to be 'ALL_USERS' for this test via the settings object
    monkeypatch.setattr(settings, "INGESTION_WORKER_S3_SYNC_MODE", "ALL_USERS")

    blacklisted_users = ["user-to-skip"]
    monkeypatch.setattr(settings, "INGESTION_WORKER_S3_SYNC_BLACKLIST_USER_IDS", blacklisted_users)

    # Get mocks from the fixture
    mock_redis_instance = mock_dependencies["redis_instance"]
    mock_users_collection = mock_dependencies["users_collection"]

    # Configure the cursor to return an empty list, as if the DB filter worked perfectly.
    mock_cursor = MagicMock()
    mock_cursor.to_list = AsyncMock(return_value=[])  # Simulate DB returned no users
    mock_users_collection.find.return_value = mock_cursor

    # ACT
    await run_nightly_s3_sync()

    # ASSERT
    # The most important assertion: verify the mongo query was called with the correct '$nin' filter on the '_id' field.
    mock_users_collection.find.assert_called_once_with({"_id": {"$nin": blacklisted_users}})

    # Verify no jobs were queued (since we mocked no users returned from DB)
    assert mock_redis_instance.lpush.call_count == 0


@pytest.mark.asyncio
async def test_nightly_sync_blacklist_overrides_whitelist(mock_dependencies, monkeypatch):
    """
    Tests that if a user is in both the whitelist and blacklist, they are SKIPPED.
    The blacklist should always take precedence for safety.
    """
    # ARRANGE
    settings = SettingsProvider.sget()
    monkeypatch.setattr(settings, "INGESTION_WORKER_S3_SYNC_MODE", "WHITELIST")

    whitelisted_users = ["user-a-whitelisted", "user-b-in-both"]
    blacklisted_users = ["user-b-in-both", "user-c-blacklisted"]

    monkeypatch.setattr(settings, "INGESTION_WORKER_S3_SYNC_WHITELIST_USER_IDS", whitelisted_users)
    monkeypatch.setattr(settings, "INGESTION_WORKER_S3_SYNC_BLACKLIST_USER_IDS", blacklisted_users)

    mock_storage = mock_dependencies["storage"]
    mock_redis_instance = mock_dependencies["redis_instance"]
    mock_processed_files_collection = mock_dependencies["processed_files_collection"]

    # Configure S3 to only return a file for the user we expect to be processed.
    mock_storage.list = AsyncMock(return_value=[MagicMock(key="files/user-a-whitelisted/doc.pdf")])
    mock_processed_files_collection.find_one = AsyncMock(return_value=None)
    mock_processed_files_collection.update_one = AsyncMock()

    async def mock_get_filename(key):
        return key.split("/")[-1]

    mock_storage.get_filename = AsyncMock(side_effect=mock_get_filename)

    # ACT
    await run_nightly_s3_sync()

    # ASSERT
    # Only one job should be queued (for "user-a-whitelisted").
    assert mock_redis_instance.lpush.call_count == 1

    # Verify the payload is for the correct user.
    call_payload = json.loads(mock_redis_instance.lpush.call_args[0][1])
    assert call_payload["user_id"] == "user-a-whitelisted"


@pytest.mark.asyncio
async def test_nightly_sync_disabled_mode_exits_early(mock_dependencies, monkeypatch):
    """
    Tests that DISABLED mode exits immediately without processing any users.
    """
    # ARRANGE
    settings = SettingsProvider.sget()
    monkeypatch.setattr(settings, "INGESTION_WORKER_S3_SYNC_MODE", "DISABLED")

    mock_users_collection = mock_dependencies["users_collection"]
    mock_redis_instance = mock_dependencies["redis_instance"]

    # ACT
    await run_nightly_s3_sync()

    # ASSERT
    # Should not query the database at all
    mock_users_collection.find.assert_not_called()

    # Should not queue any jobs or initialize object storage.
    assert mock_redis_instance.lpush.call_count == 0
    mock_dependencies["get_public_storage"].assert_not_called()


@pytest.mark.asyncio
async def test_nightly_sync_empty_blacklist_processes_all_whitelisted(
    mock_dependencies, monkeypatch
):
    """
    Tests that an empty blacklist doesn't break the filtering logic.
    All whitelisted users should be processed normally.
    """
    # ARRANGE
    settings = SettingsProvider.sget()
    monkeypatch.setattr(settings, "INGESTION_WORKER_S3_SYNC_MODE", "WHITELIST")
    monkeypatch.setattr(settings, "INGESTION_WORKER_S3_SYNC_WHITELIST_USER_IDS", ["user-a"])
    monkeypatch.setattr(settings, "INGESTION_WORKER_S3_SYNC_BLACKLIST_USER_IDS", [])

    mock_storage = mock_dependencies["storage"]
    mock_redis_instance = mock_dependencies["redis_instance"]
    mock_processed_files_collection = mock_dependencies["processed_files_collection"]

    # Mock S3 to return one file
    mock_storage.list = AsyncMock(return_value=[MagicMock(key="files/user-a/doc.pdf")])
    mock_processed_files_collection.find_one = AsyncMock(return_value=None)
    mock_processed_files_collection.update_one = AsyncMock()

    async def mock_get_filename(key):
        return key.split("/")[-1]

    mock_storage.get_filename = AsyncMock(side_effect=mock_get_filename)

    # ACT
    await run_nightly_s3_sync()

    # ASSERT
    assert mock_redis_instance.lpush.call_count == 1, (
        "Should process the one whitelisted user with empty blacklist"
    )
