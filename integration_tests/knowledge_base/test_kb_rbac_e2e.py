# tests/knowledge_base/test_kb_rbac_e2e.py

import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from tenacity import retry, stop_after_delay, wait_fixed

from src.autobots.api.v1.api_ingestion.api_ingestion import router as main_ingestion_router

# --- Import Dependencies to Override ---
from src.autobots.auth.security import get_access_token, get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.settings import SettingsProvider
from src.autobots.ingestion.job_status import JobState

# --- Import App and Core Logic ---
from src.autobots.ingestion_worker import app as worker_app
from src.autobots.user.user_orm_model import UserORM

# --- DEFENSE LAYER 1: HARD SKIP ---
# Comment out the line below to run this test locally.
pytestmark = pytest.mark.skip(
    "Skipping E2E tests in CI/CD by default. Enable manually for local testing."
)

# --- DEFENSE LAYER 2: IMPORT-TIME GUARD ---
# This runs during Test Collection, BEFORE conftest.py session fixtures.
# It is the only way to stop conftest.py from wiping the DB if Layer 1 is removed.
# isort: off
# fmt: off

_settings = SettingsProvider.sget()
_conn_str = _settings.MONGO_CONN.lower()

# If we detect Atlas (mongodb.net) and we are not in a safe environment, KILL the process.
if "mongodb.net" in _conn_str:
    # Use stderr to guarantee visibility even if pytest captures stdout
    sys.stderr.write(f"\n\n🚨 FATAL: Detected Production Connection in E2E Test: {_settings.MONGO_CONN}\n")
    sys.stderr.write("SAFETY STOP: Aborting test run immediately to protect production data.\n\n")
    pytest.exit("Aborting due to production database connection", returncode=1)
# fmt: on
# isort: on


# --- GRACEFUL HARNESS IMPORT ---
# This makes the test file agnostic to the presence of the test harness.
try:
    from src.autobots.api.deps import get_current_user as harness_dependency
except ImportError:
    harness_dependency = None


# --- DB Fixture ---
@pytest_asyncio.fixture
async def e2e_db():
    settings = SettingsProvider.sget()
    client = AsyncIOMotorClient(f"{settings.MONGO_CONN}?retryWrites=true&w=majority")
    db = client[settings.MONGO_DATABASE]
    yield db
    client.close()


# --- Test Users ---
@pytest.fixture(scope="module")
def user_owner() -> UserORM:
    return UserORM(id="11111111-1111-1111-1111-111111111111", email="e2e_owner@test.com")


@pytest.fixture(scope="module")
def user_member() -> UserORM:
    return UserORM(id="22222222-2222-2222-2222-222222222222", email="e2e_member@test.com")


@pytest.fixture(scope="module")
def user_outsider() -> UserORM:
    return UserORM(id="33333333-3333-3333-3333-333333333333", email="e2e_outsider@test.com")


class MockUserResponse:
    def __init__(self, user: UserORM):
        self.user = user


# --- Auth Override Fixture (Harness-Agnostic) ---
@pytest_asyncio.fixture
async def authenticated_client_factory(e2e_db: AsyncIOMotorDatabase):
    """
    Factory that provides a context-managed, authenticated AsyncClient.
    This fixture is HARNESS-AGNOSTIC: it overrides both the production and
    (if available) test harness authentication dependencies.
    """
    is_mounted = any(r.path == "/test-mount" for r in worker_app.routes)
    if not is_mounted:
        worker_app.include_router(main_ingestion_router, prefix="/test-mount")

    original_overrides = worker_app.dependency_overrides.copy()

    @asynccontextmanager
    async def _factory(user: UserORM):
        # 1. Define override functions
        async def override_get_user_from_access_token():
            return MockUserResponse(user=user)

        async def override_get_current_user():
            return user

        async def override_get_db():
            yield e2e_db

        # 2. Apply overrides
        worker_app.dependency_overrides[get_offline_user_from_access_token] = (
            override_get_user_from_access_token
        )
        worker_app.dependency_overrides[get_mongo_db] = override_get_db
        worker_app.dependency_overrides[get_access_token] = lambda: "dummy_e2e_token"

        # 3. Conditionally apply the harness override
        if harness_dependency:
            worker_app.dependency_overrides[harness_dependency] = override_get_current_user

        transport = ASGITransport(app=worker_app)
        async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
            yield client

    yield _factory
    # Restore original state after tests in this module are done
    worker_app.dependency_overrides = original_overrides


@pytest_asyncio.fixture(autouse=True)
async def cleanup_e2e_data(e2e_db: AsyncIOMotorDatabase):
    """Cleans up DB data before each test."""
    # Clean up both Shared (Lifecycle test) and Bulk (New test) KBs
    await e2e_db["KnowledgeBases"].delete_many({"name": {"$regex": r"^E2E_(Shared|Bulk)_KB"}})
    await e2e_db["KnowledgeBaseMemberships"].delete_many({})
    # Clean up jobs from the lifecycle test
    await e2e_db["IngestionJobs"].delete_many({"original_filename": "e2e_test_doc.txt"})

    # Clean up any random bulk users from previous runs to prevent accumulation
    await e2e_db["Users"].delete_many({"email": {"$regex": r"^bulk\d+_"}})

    # --- Seed Users for Email Enrichment ---
    # Ensures that the Users collection has the necessary documents for the API to join against.
    users_to_seed = [
        ("11111111-1111-1111-1111-111111111111", "e2e_owner@test.com"),
        ("22222222-2222-2222-2222-222222222222", "e2e_member@test.com"),
        ("33333333-3333-3333-3333-333333333333", "e2e_outsider@test.com"),
    ]
    for user_id, email in users_to_seed:
        await e2e_db["Users"].replace_one(
            {"_id": user_id},
            {"_id": user_id, "user_id": user_id, "email": email, "created_at": datetime.utcnow()},
            upsert=True,
        )
    yield


# --- The E2E Test ---


@pytest.mark.asyncio
async def test_shared_kb_lifecycle_e2e(
    authenticated_client_factory, user_owner, user_member, user_outsider, e2e_db
):
    """
    Validates the full RBAC lifecycle against a running stack.
    This test assumes a real ingestion worker is running and consuming from Redis.
    """
    # 1. SETUP: Owner creates KB & adds Member
    async with authenticated_client_factory(user_owner) as client:
        kb_name = f"E2E_Shared_KB_{uuid.uuid4().hex[:6]}"
        resp = await client.post("/api/v1_ingestion/kb/", json={"name": kb_name})
        assert resp.status_code == 201
        kb_data = resp.json()
        kb_id = kb_data["_id"]

        resp = await client.post(
            f"/api/v1_ingestion/kb/{kb_id}/members",
            json={"user_id": str(user_member.id), "role": "read_write"},
        )
        assert resp.status_code == 200

    # 1.5. VERIFY ENRICHED LISTS (New Coverage)
    async with authenticated_client_factory(user_owner) as client:
        # A. Check the main KB list for stats
        resp_list = await client.get("/api/v1_ingestion/kb/")
        assert resp_list.status_code == 200
        kb_list_data = resp_list.json()
        target_kb = next((kb for kb in kb_list_data if kb["_id"] == kb_id), None)
        assert target_kb is not None, "Newly created KB not found in list"
        assert target_kb["file_count"] == 0
        assert target_kb["member_count"] == 2  # Owner + Member
        assert target_kb["current_user_role"] == "admin"

        # B. Check the members list for emails and owner row
        resp_members = await client.get(f"/api/v1_ingestion/kb/{kb_id}/members")
        assert resp_members.status_code == 200
        members_data = resp_members.json()
        assert len(members_data) == 2
        owner_row = next((m for m in members_data if m["is_owner"]), None)
        member_row = next((m for m in members_data if not m["is_owner"]), None)
        assert owner_row is not None and owner_row["email"] == user_owner.email
        assert member_row is not None and member_row["email"] == user_member.email
        print("✅ Enriched list and member endpoints verified.")

    # 2. INGESTION: Member uploads a file to the shared KB
    async with authenticated_client_factory(user_member) as client:
        files = {"file": ("e2e_test_doc.txt", b"E2E RBAC Test Content.", "text/plain")}
        resp = await client.post(f"/test-mount/ingestion/file/async?kb_id={kb_id}", files=files)
        assert resp.status_code == 200, f"Upload failed: {resp.text}"
        job_id = resp.json()["job_id"]
        print(f"✅ Upload success. Job ID: {job_id}")

    # 3. POLLING: Wait for the REAL worker to process the job by polling the DB
    @retry(stop=stop_after_delay(120), wait=wait_fixed(3), reraise=True)
    async def _poll_for_job_completion():
        print(f"Polling for job {job_id}...")
        job_doc = await e2e_db["IngestionJobs"].find_one({"_id": job_id})
        assert job_doc is not None, "Job document not found in DB"

        status = job_doc.get("status")
        if status == JobState.FAILED.value:
            pytest.fail(f"Ingestion job {job_id} failed: {job_doc.get('error_message')}")

        assert status == JobState.AVAILABLE.value, (
            f"Job {job_id} status is {status}, not AVAILABLE."
        )
        print(f"✅ Job {job_id} is AVAILABLE.")
        return job_doc

    final_job_doc = await _poll_for_job_completion()
    assert final_job_doc["tenant_id"] == kb_id
    assert final_job_doc["uploaded_by_user_id"] == str(user_member.id)

    # 4. VERIFY LISTING (Owner sees Member's file)
    async with authenticated_client_factory(user_owner) as client:
        resp = await client.get(f"/api/v1_ingestion/files/?scope=kb&kb_id={kb_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        found_job = next((f for f in data["files"] if f["_id"] == job_id), None)
        assert found_job is not None, "Owner could not see Member's file in listing"
        print("✅ Owner can see member's file in shared KB.")

    # 5. VERIFY SEARCH ISOLATION
    # Outsider blocked from search
    async with authenticated_client_factory(user_outsider) as client:
        search_payload = {
            "query": "test",
            "collection_name": "synced_files",
            "kb_id": kb_id,
            "scope": "kb",
            "vector_search_top_k": 1,
            "graph_traversal_depth": 1,
            "docs_context_top_k": 0,
        }
        resp = await client.post("/api/v1_ingestion/knowledge-base/search", json=search_payload)
        assert resp.status_code == 403
        print("✅ Outsider correctly blocked from searching shared KB.")

    # 6. REVOCATION FLOW
    # Owner removes member
    async with authenticated_client_factory(user_owner) as client:
        resp = await client.delete(f"/api/v1_ingestion/kb/{kb_id}/members/{user_member.id}")
        assert resp.status_code == 204
        print("✅ Member removed from KB.")

    # Verify Ex-Member cannot access files anymore
    async with authenticated_client_factory(user_member) as client:
        resp = await client.get(f"/api/v1_ingestion/files/?scope=kb&kb_id={kb_id}")
        # Should now be 403 Forbidden
        assert resp.status_code == 403
        print("✅ Ex-Member correctly blocked from listing files.")

    # 7. VERIFY ISOLATION (Outsider is blocked)
    async with authenticated_client_factory(user_outsider) as client:
        resp = await client.get(f"/api/v1_ingestion/files/?scope=kb&kb_id={kb_id}")
        assert resp.status_code == 403
        print("✅ Outsider correctly blocked from shared KB.")

    print("✅ E2E RBAC flow passed.")


@pytest.mark.asyncio
async def test_bulk_member_management_worker_smoke(
    authenticated_client_factory, user_owner, e2e_db
):
    """
    Smoke test for the Worker's Bulk Member Add endpoint in the live environment.
    Verifies that the endpoint persists data correctly to the real DB.
    """
    # 1. Setup Data
    kb_name = f"E2E_Bulk_KB_{uuid.uuid4().hex[:6]}"

    # Create 2 target users in the E2E DB with valid UUIDs
    u1_id = str(uuid.uuid4())
    u2_id = str(uuid.uuid4())
    u1_email = f"bulk1_{uuid.uuid4().hex[:4]}@test.com"
    u2_email = f"bulk2_{uuid.uuid4().hex[:4]}@test.com"

    await e2e_db["Users"].insert_many(
        [
            {"_id": u1_id, "user_id": u1_id, "email": u1_email, "created_at": datetime.utcnow()},
            {"_id": u2_id, "user_id": u2_id, "email": u2_email, "created_at": datetime.utcnow()},
        ]
    )

    async with authenticated_client_factory(user_owner) as client:
        # 2. Create KB
        resp = await client.post("/api/v1_ingestion/kb/", json={"name": kb_name})
        assert resp.status_code == 201
        kb_id = resp.json()["_id"]

        # 3. Bulk Add
        payload = {"emails": [u1_email, u2_email, "ghost@test.com"], "role": "read_write"}
        resp = await client.post(
            f"/api/v1_ingestion/kb/{kb_id}/members/bulk-by-email", json=payload
        )
        assert resp.status_code == 200
        data = resp.json()

        # 4. Verify Response
        assert data["inserted_count"] == 2
        assert "ghost@test.com" in data["not_found_emails"]

        # 5. Verify Persistence via List Endpoint
        resp_list = await client.get(f"/api/v1_ingestion/kb/{kb_id}/members")
        assert resp_list.status_code == 200
        members = resp_list.json()

        # Should be 3: Owner + 2 Bulk Added
        assert len(members) == 3

        # Check roles
        bulk_members = [m for m in members if not m["is_owner"]]
        assert all(m["role"] == "read_write" for m in bulk_members)

    print("✅ E2E Bulk Member Add verified.")
