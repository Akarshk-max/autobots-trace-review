# tests/api/v1_ingestion/test_api_file_status.py

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import supabase_auth
from httpx import ASGITransport, AsyncClient
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.ingestion.job_status import BackendState, JobState, JobStatusCreate
from src.autobots.ingestion_worker import app  # Import the app from the worker
from src.autobots.user.user_orm_model import UserORM

# --- New Fixture for the Test Client ---


# Create a specific user fixture for this test module to avoid ScopeMismatch
# with the session-scoped 'user' fixture in conftest.py
@pytest.fixture(scope="function")
def file_status_test_user():
    # Use a STATIC ID to guarantee the seed and the client see the exact same user
    return UserORM(id="123e4567-e89b-12d3-a456-426614174000", email="test_status_api@example.com")


@pytest_asyncio.fixture(scope="function")
async def client(file_status_test_user: UserORM, db: AsyncIOMotorDatabase) -> AsyncClient:
    """
    Provides an httpx.AsyncClient configured to talk to the test app.
    """

    # Override Auth: Return the local test user
    async def override_get_user_from_access_token():
        return supabase_auth.UserResponse(user=file_status_test_user)

    app.dependency_overrides[get_offline_user_from_access_token] = override_get_user_from_access_token

    # Override DB: Ensure API uses the same DB connection as the test fixture
    async def override_get_mongo_db():
        yield db

    app.dependency_overrides[get_mongo_db] = override_get_mongo_db

    # The ASGITransport allows httpx to call the app directly
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as c:
        yield c

    # Clean up the override after the test
    app.dependency_overrides.clear()


# --- Test Data Fixture ---


@pytest_asyncio.fixture(scope="function", autouse=True)
async def seed_test_jobs(db, file_status_test_user):
    """
    Cleans the IngestionJobs collection and seeds it with a diverse
    set of jobs for testing. Runs automatically for every test in this file.
    """
    collection = db["IngestionJobs"]
    await collection.delete_many(
        {"user_id": file_status_test_user.id}
    )  # Clean up previous test runs

    # DEBUG: Print user ID to ensure match
    print(f"DEBUG: Seeding jobs for user_id: {file_status_test_user.id}")

    base_time = datetime.now(UTC)

    # User for this test
    test_user_id = file_status_test_user.id

    # A different user to test data isolation
    other_user_id = "other-user-id"

    # Helper to create valid job documents using the Pydantic model
    def create_job_doc(id, uid, filename, status, w_status, g_status, created_at, **kwargs):
        # 1. Create valid model (handles defaults and required fields)
        job = JobStatusCreate(
            _id=id,
            user_id=uid,
            original_filename=filename,
            status=status,
            weaviate_status=w_status,
            graphrag_status=g_status,
            # Pass any extra fields like error_message
            **kwargs,
        )
        # 2. Dump to dict (by_alias=True keeps _id)
        doc = job.model_dump(by_alias=True)
        # 3. Manually override timestamps (Pydantic defaults to now())
        doc["created_at"] = created_at
        doc["updated_at"] = created_at
        return doc

    jobs_to_insert = [
        # 1. Fully successful job
        create_job_doc(
            "job_available",
            test_user_id,
            "Q1_Financial_Report.pdf",
            JobState.AVAILABLE,
            BackendState.AVAILABLE,
            BackendState.AVAILABLE,
            base_time - timedelta(days=2),
        ),
        # 2. Partially successful job
        create_job_doc(
            "job_partial",
            test_user_id,
            "Sales_Presentation_Q1.pptx",
            JobState.PARTIAL_AVAILABLE,
            BackendState.AVAILABLE,
            BackendState.FAILED,
            base_time - timedelta(days=1),
            error_message="GraphRAG processing failed.",
        ),
        # 3. Fully failed job
        create_job_doc(
            "job_failed",
            test_user_id,
            "annual_data.xlsx",
            JobState.FAILED,
            BackendState.FAILED,
            BackendState.FAILED,
            base_time - timedelta(hours=6),
            error_message="Weaviate readiness probe timed out.",
        ),
        # 4. Job currently processing
        create_job_doc(
            "job_processing",
            test_user_id,
            "Draft_Proposal.docx",
            JobState.PROCESSING,
            BackendState.PROCESSING,
            BackendState.QUEUED,
            base_time - timedelta(minutes=10),
        ),
        # 5. Job belonging to another user (for isolation test)
        create_job_doc(
            "job_other_user",
            other_user_id,
            "secret_document.pdf",
            JobState.AVAILABLE,
            BackendState.AVAILABLE,
            BackendState.AVAILABLE,
            base_time,
        ),
    ]
    await collection.insert_many(jobs_to_insert)

    # DEBUG: Verify insertion
    count = await collection.count_documents({"user_id": test_user_id})
    print(f"DEBUG: Inserted {count} jobs for {test_user_id}")
    assert count == 4, "Fixture failed to insert jobs!"
    yield  # Test runs here

    # Teardown
    await collection.delete_many({"user_id": {"$in": [test_user_id, other_user_id]}})


# --- API Tests ---


# Update the test function signature to accept the new fixture name if needed,
# but 'client' already encapsulates the user logic via the override.
@pytest.mark.asyncio
async def test_list_files_isolation_and_basic(client: AsyncClient):
    """
    Tests data isolation (only own files are returned) and the basic response structure.
    """
    headers = {"Authorization": "Bearer fake-token-for-testing"}
    response = await client.get("/api/v1_ingestion/files/", headers=headers)

    assert response.status_code == 200
    data = response.json()

    assert data["total"] == 4  # Should only count the 4 jobs for the test_user
    assert len(data["files"]) == 4
    assert data["limit"] == 50
    assert data["offset"] == 0

    # Check that the other user's file is not present
    filenames = [f["original_filename"] for f in data["files"]]
    assert "secret_document.pdf" not in filenames


@pytest.mark.asyncio
async def test_list_files_pagination(client: AsyncClient):
    """Tests the limit and offset parameters."""
    headers = {"Authorization": "Bearer fake-token-for-testing"}
    response = await client.get("/api/v1_ingestion/files/?limit=2&offset=1", headers=headers)

    assert response.status_code == 200
    data = response.json()

    assert data["total"] == 4
    assert data["limit"] == 2
    assert data["offset"] == 1
    assert len(data["files"]) == 2
    # The default sort is by created_at desc, so offset=1 should skip the newest job ("job_processing")
    # and start with the next one ("job_failed")
    assert data["files"][0]["_id"] == "job_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "search_term, expected_count",
    [
        ("Q1", 2),
        ("report", 1),
        ("q1_financial", 1),  # Test case-insensitivity
        ("nonexistent", 0),
    ],
)
async def test_filter_by_filename(client: AsyncClient, search_term, expected_count):
    """Tests case-insensitive filename search."""
    headers = {"Authorization": "Bearer fake-token-for-testing"}
    response = await client.get(
        f"/api/v1_ingestion/files/?filename_contains={search_term}", headers=headers
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == expected_count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, expected_id",
    [
        ("available", "job_available"),
        ("failed", "job_failed"),
        ("partial_available", "job_partial"),
        ("processing", "job_processing"),
    ],
)
async def test_filter_by_status(client: AsyncClient, status, expected_id):
    """Tests filtering by various overall statuses."""
    headers = {"Authorization": "Bearer fake-token-for-testing"}
    response = await client.get(f"/api/v1_ingestion/files/?status={status}", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["files"][0]["_id"] == expected_id


@pytest.mark.asyncio
async def test_filter_by_backend_status(client: AsyncClient):
    """Tests filtering by specific backend statuses."""
    headers = {"Authorization": "Bearer fake-token-for-testing"}
    # Find files where Weaviate is ready but GraphRAG failed
    response = await client.get(
        "/api/v1_ingestion/files/?weaviate_status=available&graphrag_status=failed", headers=headers
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["files"][0]["_id"] == "job_partial"


@pytest.mark.asyncio
async def test_filter_by_date_range(client: AsyncClient):
    """Tests filtering by created_at date range."""
    start_date = (datetime.now(UTC) - timedelta(days=1, hours=6)).isoformat()
    end_date = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    params = {"start_date": start_date, "end_date": end_date}
    headers = {"Authorization": "Bearer fake-token-for-testing"}
    response = await client.get("/api/v1_ingestion/files/", headers=headers, params=params)

    assert response.status_code == 200
    data = response.json()
    # Should match job_partial and job_failed
    assert data["total"] == 2
    filenames = {f["original_filename"] for f in data["files"]}
    assert "Sales_Presentation_Q1.pptx" in filenames
    assert "annual_data.xlsx" in filenames


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sort_by, sort_order, expected_first_id",
    [
        ("created_at", "desc", "job_processing"),
        ("created_at", "asc", "job_available"),
        ("updated_at", "desc", "job_processing"),
        ("updated_at", "asc", "job_available"),
        ("original_filename", "asc", "job_processing"),  # Ascii: 'D' (Draft...) < 'Q' < 'S' < 'a'
        ("original_filename", "desc", "job_failed"),  # Ascii: 'a' (annual...) > 'S' > 'Q' > 'D'
        ("status", "asc", "job_available"),  # Alphabetical: AVAILABLE, FAILED, ...
        ("status", "desc", "job_processing"),  # Alphabetical: PROCESSING, PARTIAL_AVAILABLE, ...
    ],
)
async def test_sorting(client: AsyncClient, sort_by, sort_order, expected_first_id):
    """Tests various sorting combinations."""
    headers = {"Authorization": "Bearer fake-token-for-testing"}
    response = await client.get(
        f"/api/v1_ingestion/files/?sort_by={sort_by}&sort_order={sort_order}", headers=headers
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["files"]) > 0
    assert data["files"][0]["_id"] == expected_first_id


@pytest.mark.asyncio
async def test_invalid_filter_value_returns_400(client: AsyncClient):
    """Ensures invalid enum values are rejected."""
    headers = {"Authorization": "Bearer fake-token-for-testing"}
    response = await client.get(
        "/api/v1_ingestion/files/?status=invalid-status-value", headers=headers
    )

    assert response.status_code == 400
    assert "Invalid filter value" in response.json()["detail"]
