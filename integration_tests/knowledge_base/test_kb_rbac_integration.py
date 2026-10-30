# tests/knowledge_base/test_kb_rbac_integration.py

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from src.autobots.api.v1.api_ingestion.api_ingestion import router as main_ingestion_router

# Import dependencies to override
from src.autobots.auth.security import get_access_token, get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.settings import SettingsProvider

# We are testing the worker's API, so we import its app instance
from src.autobots.ingestion_worker import app as worker_app
from src.autobots.knowledge_base.kb_doc_model import KnowledgeBaseRole
from src.autobots.knowledge_base.kb_service import (
    resolve_kb_id_by_name_for_user,
    resolve_tenant_id_and_check_access,
)

# Import data models and logic
from src.autobots.user.user_orm_model import UserORM

# --- DEFENSE LAYER 1: HARD SKIP ---
# Comment out the line below to run this test locally.
pytestmark = pytest.mark.skip(
    "Skipping destructive integration tests by default. Enable manually for local testing."
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
    sys.stderr.write(f"\n\n🚨 FATAL: Detected Production Connection: {_settings.MONGO_CONN}\n")
    pytest.exit("SAFETY STOP: Aborting test run immediately to protect production data.", returncode=1)
# fmt: on
# isort: on

# --- 1. SETUP ENV VARS BEFORE IMPORTING APP/SETTINGS ---
# This ensures boto3 finds "credentials" and Pydantic finds required fields.
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

# Required by Settings class
os.environ["AWS_S3_BUCKET_NAME"] = "test-bucket"
os.environ["AWS_S3_BUCKET_REGION"] = "us-east-1"
os.environ["AWS_S3_PUBLIC_BUCKET_NAME"] = "test-public-bucket"
os.environ["AWS_S3_PUBLIC_BUCKET_IMAGE_FOLDER"] = "test-images"

# --- DYNAMIC ROUTER MOUNTING ---
# The ingestion endpoints (upload) live in api_ingestion.py, which is normally
# part of the Main App. We import it here to mount it on the Worker App for testing.


# --- GRACEFUL HARNESS IMPORT ---
# This makes the test file agnostic to the presence of the test harness.
try:
    from src.autobots.api.deps import get_current_user as harness_dependency
except ImportError:
    harness_dependency = None

# --- LOCAL FIXTURES TO ENSURE LOOP CONSISTENCY ---
# We redefine these here to guarantee they use the current test loop,
# making the test robust against changes in the global conftest.py.


@pytest_asyncio.fixture
async def local_motor_client():
    """
    Creates a Motor client explicitly attached to the current running loop.
    This overrides any session-scoped client from conftest.py for this file.
    """
    settings = SettingsProvider.sget()
    # Explicitly get the loop that pytest-asyncio is providing for this test
    loop = asyncio.get_running_loop()
    client = AsyncIOMotorClient(f"{settings.MONGO_CONN}?retryWrites=true&w=majority", io_loop=loop)
    yield client
    client.close()


@pytest_asyncio.fixture
async def local_db(local_motor_client):
    """Provides the database object from the local, loop-aligned client."""
    settings = SettingsProvider.sget()
    return local_motor_client[settings.MONGO_DATABASE]


# --- Test User Fixtures ---
# These represent the different roles in our RBAC scenarios.


@pytest.fixture(scope="module")
def user_owner() -> UserORM:
    """A user who will own the Knowledge Base."""
    return UserORM(id="00000000-0000-0000-0000-000000000001", email="owner@test.com")


@pytest.fixture(scope="module")
def user_member_read() -> UserORM:
    """A user who will be a member with READ-only access."""
    return UserORM(id="00000000-0000-0000-0000-000000000002", email="member_read@test.com")


@pytest.fixture(scope="module")
def user_member_write() -> UserORM:
    """A user who will be a member with READ-WRITE access."""
    return UserORM(id="00000000-0000-0000-0000-000000000003", email="member_write@test.com")


@pytest.fixture(scope="module")
def user_outsider() -> UserORM:
    """A user with no connection to the Knowledge Base."""
    return UserORM(id="00000000-0000-0000-0000-000000000004", email="outsider@test.com")


# --- A simple mock class to mimic supabase_auth.UserResponse ---
class MockUserResponse:
    def __init__(self, user: UserORM):
        self.user = user


# --- Core Fixture for Authentication Override ---
@pytest_asyncio.fixture
async def authenticated_client_factory(local_db: AsyncIOMotorDatabase):
    """
    A factory that provides a context-managed, authenticated AsyncClient.
    This fixture is HARNESS-AGNOSTIC: it overrides both the production and
    (if available) test harness authentication dependencies.
    It overrides get_mongo_db to force the app to use our local_db.
    """

    # Store original dependencies to restore them after the test
    original_prod_auth = worker_app.dependency_overrides.get(get_offline_user_from_access_token)
    original_token_auth = worker_app.dependency_overrides.get(get_access_token)
    original_harness_auth = (
        worker_app.dependency_overrides.get(harness_dependency) if harness_dependency else None
    )
    original_db_dep = worker_app.dependency_overrides.get(get_mongo_db)

    # --- MOUNT MAIN APP ROUTER FOR TESTING ---
    # We mount the main app's ingestion router onto the worker app
    # so we can test the upload RBAC logic in this harness.
    # Path becomes: /test-mount/ingestion/file/async
    # We check existing routes to avoid "router already mounted" errors on repeated runs
    is_mounted = any(r.path == "/test-mount" for r in worker_app.routes)
    if not is_mounted:
        worker_app.include_router(main_ingestion_router, prefix="/test-mount")

    @asynccontextmanager
    async def _factory(user: UserORM):
        # 1. Override Auth (Prod style)
        async def override_get_user_from_access_token():
            return MockUserResponse(user=user)

        # 2. Override Auth (Harness style)
        async def override_get_current_user():
            return user

        # 3. Override Token Auth
        async def override_get_access_token():
            return "dummy_test_token"

        # 4. Override DB
        # Force the app to use the same DB connection as the test functions.
        # This prevents the app from trying to create a new connection on a different loop.
        async def override_get_mongo_db():
            yield local_db

        # Apply overrides
        worker_app.dependency_overrides[get_offline_user_from_access_token] = (
            override_get_user_from_access_token
        )
        worker_app.dependency_overrides[get_access_token] = override_get_access_token

        if harness_dependency:
            worker_app.dependency_overrides[harness_dependency] = override_get_current_user
        worker_app.dependency_overrides[get_mongo_db] = override_get_mongo_db

        # 4. Use ASGITransport
        transport = ASGITransport(app=worker_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    yield _factory

    # Teardown: Restore or clear
    worker_app.dependency_overrides.clear()
    if original_prod_auth:
        worker_app.dependency_overrides[get_offline_user_from_access_token] = original_prod_auth
    if original_token_auth:
        worker_app.dependency_overrides[get_access_token] = original_token_auth
    if harness_dependency and original_harness_auth:
        worker_app.dependency_overrides[harness_dependency] = original_harness_auth
    if original_db_dep:
        worker_app.dependency_overrides[get_mongo_db] = original_db_dep


@pytest_asyncio.fixture(autouse=True)
async def db_cleanup(local_db: AsyncIOMotorDatabase):
    """
    Ensures that collections created during RBAC tests are dropped after each test function.
    This provides test isolation, which is crucial for security testing.
    """
    yield
    # Your conftest already sets up a lot of data. We will only clean up what we create.
    await local_db["KnowledgeBases"].delete_many({})
    await local_db["KnowledgeBaseMemberships"].delete_many({})
    # Also clean up jobs to test file listing scopes cleanly
    await local_db["IngestionJobs"].delete_many({})
    await local_db["processed_files_log"].delete_many({})

    # --- Clean up specific test users to ensure isolation ---
    test_user_ids = [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        "00000000-0000-0000-0000-000000000003",
        "00000000-0000-0000-0000-000000000004",
    ]
    await local_db["Users"].delete_many({"_id": {"$in": test_user_ids}})

    # Clean up extra users created by bulk tests
    bulk_test_emails = ["valid@test.com", "update@test.com", "legacy@test.com"]
    await local_db["Users"].delete_many({"email": {"$in": bulk_test_emails}})
    print("🧹 Cleaned up RBAC test collections.")


# --- FIXTURE: Seed Users ---
@pytest_asyncio.fixture
async def seed_users(local_db, user_owner, user_member_read, user_member_write, user_outsider):
    """Ensures user documents exist in Mongo for email lookup tests."""
    users = [user_owner, user_member_read, user_member_write, user_outsider]
    for u in users:
        # --- FIX: Store _id as a string to match application query patterns ---
        uid = str(u.id)
        await local_db["Users"].replace_one(
            {"_id": uid},
            {
                "_id": uid,
                "user_id": uid,
                "email": u.email.lower(),
                "created_at": datetime.utcnow(),  # Use real datetime
            },
            upsert=True,
        )


# --- HELPER: Create KB ---
async def create_kb_helper(client, name="Test KB"):
    resp = await client.post("/api/v1_ingestion/kb/", json={"name": name})
    assert resp.status_code == 201
    return resp.json()


# --- HELPER: Mock S3 & Redis for Ingestion ---
@pytest.fixture
def mock_ingestion_deps():
    """
    Patches UserFiles.upload_files to bypass S3 logic and Redis to allow queueing.
    This focuses the test on API-level RBAC and DB operations.
    """
    # 1. Patch UserFiles.upload_files where it is USED in the api_ingestion module
    # We use AsyncMock because it's an async method
    with (
        patch(
            "src.autobots.api.v1.api_ingestion.api_ingestion.UserFiles.upload_files",
            new_callable=AsyncMock,
        ) as mock_upload,
        patch("src.autobots.api.v1.api_ingestion.api_ingestion.get_public_s3") as mock_get_s3,
        patch("src.autobots.api.v1.api_ingestion.api_ingestion.get_redis") as mock_redis_get,
    ):
        # Configure upload_files to return what the endpoint expects: a list of FileMeta objects
        mock_meta = MagicMock()
        mock_meta.url = "https://mock-s3.com/bucket/file.pdf"
        mock_upload.return_value = [mock_meta]

        # Configure get_public_s3 to return a basic mock.
        # Even though we bypass upload_files, the endpoint calls get_public_s3()
        # BEFORE creating UserFiles to pass it to the constructor.
        # We also need it for the 'get_key_from_https_url' call later in the endpoint.
        mock_s3_client = MagicMock()

        # Async methods on the s3 client must be awaitable
        async def mock_get_key(*args, **kwargs):
            return "files/user/file.pdf"

        mock_s3_client.get_key_from_https_url.side_effect = mock_get_key

        # Mock the low-level boto client used for copy_object
        mock_s3_client.s3_client.copy_object.return_value = {}

        # Mock delete for cleanup
        async def mock_delete(*args, **kwargs):
            return {}

        mock_s3_client.delete.side_effect = mock_delete

        mock_get_s3.return_value = mock_s3_client

        # Mock Redis
        mock_redis = MagicMock()
        mock_redis.lpush.return_value = 1
        mock_redis_get.return_value = mock_redis

        yield


# --- The First Test: Harness Check ---


@pytest.mark.asyncio
async def test_harness_owner_can_create_kb(authenticated_client_factory, user_owner, local_db):
    """
    SMOKE TEST: Verifies that the auth override works and an authenticated
    user (as owner) can successfully create a Knowledge Base. This test is
    agnostic to whether the test harness is present in the code.
    """
    # ARRANGE: Get a client authenticated as the owner using our factory
    async with authenticated_client_factory(user_owner) as client:
        kb_name = "My First Test KB"

        # ACT: Make the API call to create a KB
        # AsyncClient methods are coroutines, so we MUST await them
        response = await client.post(
            "/api/v1_ingestion/kb/", json={"name": kb_name, "description": "A test description."}
        )

        # ASSERT: Check API response
        assert response.status_code == 201, f"API call failed: {response.text}"
        response_data = response.json()

        # --- ASSERT: enriched response ---
        assert response_data["name"] == kb_name
        assert response_data["owner_user_id"] == str(user_owner.id)
        assert response_data["tenant_id"] == response_data["_id"]
        assert response_data["file_count"] == 0
        assert response_data["member_count"] == 1  # Owner is the only member
        assert response_data["current_user_role"] == "admin"

        # ASSERT: Check database state
        created_kb_doc = await local_db["KnowledgeBases"].find_one({"_id": response_data["_id"]})
        assert created_kb_doc is not None
        assert created_kb_doc["name"] == kb_name
        assert created_kb_doc["owner_user_id"] == str(user_owner.id)

    print("✅ Harness Check Passed: Auth override and DB interaction are working.")


@pytest.mark.asyncio
class TestRBACApiEndpoints:
    async def test_admin_can_manage_members(
        self, authenticated_client_factory, user_owner, user_member_read, seed_users
    ):
        """Covers: Owner can add/list members AND verify email enrichment."""
        async with authenticated_client_factory(user_owner) as client:
            # 1. Create KB
            kb_data = await create_kb_helper(client)
            kb_id = kb_data["_id"]

            # 2. Add Member (Read Only)
            member_payload = {"user_id": str(user_member_read.id), "role": "read"}
            resp = await client.post(f"/api/v1_ingestion/kb/{kb_id}/members", json=member_payload)
            assert resp.status_code == 200, resp.text
            # Assert email is present in the response after adding (as POST returns enriched model)
            assert resp.json()["email"] == user_member_read.email

            # 3. List Members
            resp = await client.get(f"/api/v1_ingestion/kb/{kb_id}/members")
            assert resp.status_code == 200, resp.text
            members = resp.json()

            # --- UPDATED ASSERTIONS for enriched member list ---
            # Should now have 2 members: the synthetic Owner row + the added Member row
            assert len(members) == 2

            # Verify Owner row
            owner_row = next((m for m in members if m.get("is_owner")), None)
            assert owner_row is not None
            assert owner_row["user_id"] == str(user_owner.id)
            assert owner_row["role"] == "admin"
            assert owner_row["email"] == user_owner.email

            # Verify Member row
            member_row = next((m for m in members if not m.get("is_owner")), None)
            assert member_row is not None
            assert member_row["user_id"] == str(user_member_read.id)
            assert member_row["role"] == "read"
            assert member_row["email"] == user_member_read.email

    async def test_bulk_add_members_exhaustive(
        self, authenticated_client_factory, user_owner, local_db, seed_users
    ):
        """
        Covers the complex logic of Bulk Add:
        1. Valid users (Added/Updated)
        2. Input Sanitization (Splitting commas)
        3. Invalid Formats (Regex check)
        4. Non-existent users (Not found)
        5. Owner protection (Skipped)
        6. Legacy users (ObjectId skipped)
        """
        # --- SETUP ---
        # 1. Create KB
        async with authenticated_client_factory(user_owner) as client:
            kb_data = await create_kb_helper(client, "Bulk Test KB")
            kb_id = kb_data["_id"]

        # 2. Seed Users for various scenarios
        # We need specific extra users for this test beyond standard seed_users
        from bson import ObjectId

        # A: Valid User (Standard)
        user_valid = UserORM(id="00000000-0000-0000-0000-000000000099", email="valid@test.com")
        await local_db["Users"].insert_one(
            {"_id": str(user_valid.id), "user_id": str(user_valid.id), "email": user_valid.email}
        )

        # B: Valid User (To be updated)
        user_update = UserORM(id="00000000-0000-0000-0000-000000000098", email="update@test.com")
        await local_db["Users"].insert_one(
            {"_id": str(user_update.id), "user_id": str(user_update.id), "email": user_update.email}
        )
        # Pre-add them as READ
        await local_db["KnowledgeBaseMemberships"].insert_one(
            {
                "_id": f"{kb_id}:{user_update.id}",
                "kb_id": kb_id,
                "user_id": str(user_update.id),
                "role": "read",
            }
        )

        # C: Legacy User (ObjectId _id, no user_id field) - Should be SKIPPED
        legacy_oid = ObjectId()
        await local_db["Users"].insert_one({"_id": legacy_oid, "email": "legacy@test.com"})

        # --- EXECUTE ---
        payload = {
            "role": "read_write",
            "emails": [
                "valid@test.com",  # Scenario A: Insert
                "update@test.com, bad-format",  # Scenario B: Update + Scenario D: Invalid (Split check)
                user_owner.email,  # Scenario E: Owner (Skip) - verified via seed_users
                "legacy@test.com",  # Scenario C: Legacy (Skip)
                "ghost@test.com",  # Scenario F: Not Found
            ],
        }

        async with authenticated_client_factory(user_owner) as client:
            resp = await client.post(
                f"/api/v1_ingestion/kb/{kb_id}/members/bulk-by-email", json=payload
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()

        # --- ASSERTIONS ---

        # 1. Counts
        # We sent 5 distinct emails (valid, update, owner, legacy, ghost) + 1 invalid format
        # The validator splits "update@test.com, bad-format" into 2 items.
        # Total items processed by logic: 6
        assert data["total_requested"] == 6

        # 2. Successes
        # valid@test.com -> Inserted
        assert data["inserted_count"] == 1

        # update@test.com -> Updated (read -> read_write)
        # Note: matched_count will be at least 1 (the update), modified_count 1.
        assert data["updated_count"] == 1
        assert data["matched_count"] >= 1

        # 3. Partial Failure Lists
        assert "bad-format" in data["invalid_emails"]
        assert "ghost@test.com" in data["not_found_emails"]
        assert (
            user_owner.email.lower() in data["skipped_owner_emails"]
        )  # seed_users normalizes email
        assert "legacy@test.com" in data["skipped_legacy_users"]

        # 4. Verify DB State
        # Valid user should be read_write
        mem_valid = await local_db["KnowledgeBaseMemberships"].find_one(
            {"kb_id": kb_id, "user_id": str(user_valid.id)}
        )
        assert mem_valid["role"] == "read_write"

        # Updated user should be read_write
        mem_update = await local_db["KnowledgeBaseMemberships"].find_one(
            {"kb_id": kb_id, "user_id": str(user_update.id)}
        )
        assert mem_update["role"] == "read_write"

        # Legacy user should NOT exist in memberships
        mem_legacy = await local_db["KnowledgeBaseMemberships"].find_one(
            {"kb_id": kb_id, "user_id": str(legacy_oid)}
        )
        assert mem_legacy is None

    async def test_bulk_add_permissions_and_limits(
        self, authenticated_client_factory, user_owner, user_member_read, local_db, seed_users
    ):
        """Covers: RBAC enforcement (403) and Input Limits (422/400)."""
        # Setup KB
        async with authenticated_client_factory(user_owner) as client:
            kb_data = await create_kb_helper(client)
            kb_id = kb_data["_id"]
            # Add read member
            await client.post(
                f"/api/v1_ingestion/kb/{kb_id}/members",
                json={"user_id": str(user_member_read.id), "role": "read"},
            )

        # 1. RBAC: Read-only member cannot bulk add
        async with authenticated_client_factory(user_member_read) as client:
            resp = await client.post(
                f"/api/v1_ingestion/kb/{kb_id}/members/bulk-by-email",
                json={"emails": ["new@test.com"], "role": "read"},
            )
            assert resp.status_code == 403

        # 2. Limits: Too many emails (>500)
        # We generate a list of 501 emails
        many_emails = [f"user{i}@test.com" for i in range(501)]
        async with authenticated_client_factory(user_owner) as client:
            resp = await client.post(
                f"/api/v1_ingestion/kb/{kb_id}/members/bulk-by-email",
                json={"emails": many_emails, "role": "read"},
            )
            # Pydantic validator raises ValueError -> FastAPI converts to 422
            assert resp.status_code == 422
            assert "Too many emails" in resp.text

    async def test_main_api_proxy_bulk_members_route_wires_correctly(
        self, authenticated_client_factory, user_owner, seed_users
    ):
        """
        Validates that the proxy route in api_ingestion.py exists and forwards correctly.
        This uses the mounted router logic in authenticated_client_factory.
        """
        kb_id = "kb_proxy_test"

        fake_worker_response = {
            "total_requested": 1,
            "inserted_count": 1,
            "updated_count": 0,
            "matched_count": 0,
            "invalid_emails": [],
            "not_found_emails": [],
            "skipped_owner_emails": [],
            "skipped_legacy_users": [],
        }

        # Patch the internal helper _proxy_request inside api_ingestion
        with patch(
            "src.autobots.api.v1.api_ingestion.api_ingestion._proxy_request",
            new_callable=AsyncMock,
        ) as mock_proxy:
            mock_proxy.return_value = fake_worker_response

            async with authenticated_client_factory(user_owner) as client:
                # Hit the mounted proxy route: /test-mount/ingestion/...
                resp = await client.post(
                    f"/test-mount/ingestion/kb/{kb_id}/members/bulk-by-email",
                    json={"emails": ["test@test.com"], "role": "read"},
                )
                assert resp.status_code == 200, resp.text
                assert resp.json()["inserted_count"] == 1

            # Assert proxy call shape
            mock_proxy.assert_awaited_once()
            args, kwargs = mock_proxy.call_args
            assert args[0] == "POST"
            assert args[1] == f"/kb/{kb_id}/members/bulk-by-email"
            assert kwargs["timeout"] == 60.0
            # Verify payload structure
            assert kwargs["json_body"]["emails"] == ["test@test.com"]
            assert kwargs["json_body"]["role"] == "read"

    async def test_non_admin_member_cannot_delete_or_patch_members(
        self,
        authenticated_client_factory,
        user_owner,
        user_member_write,
        user_member_read,
        seed_users,
    ):
        """Covers: READ_WRITE member cannot DELETE or PATCH other members (Destructive Ops)."""
        # 1. Setup KB with Owner, Write-Member, and Read-Member
        async with authenticated_client_factory(user_owner) as owner_client:
            kb_data = await create_kb_helper(owner_client, "Destructive Ops KB")
            kb_id = kb_data["_id"]
            await owner_client.post(
                f"/api/v1_ingestion/kb/{kb_id}/members",
                json={"user_id": str(user_member_write.id), "role": "read_write"},
            )
            await owner_client.post(
                f"/api/v1_ingestion/kb/{kb_id}/members",
                json={"user_id": str(user_member_read.id), "role": "read"},
            )

        # 2. Authenticate as Write-Member (Non-Admin)
        async with authenticated_client_factory(user_member_write) as client:
            # Attempt to Delete Read-Member
            resp_del = await client.delete(
                f"/api/v1_ingestion/kb/{kb_id}/members/{user_member_read.id}"
            )
            assert resp_del.status_code == 403, (
                "Non-admin member should not be able to delete members."
            )

            # Attempt to Change Role of Read-Member
            resp_patch = await client.patch(
                f"/api/v1_ingestion/kb/{kb_id}/members/{user_member_read.id}",
                json={"role": "admin"},  # Trying to escalate privilege
            )
            assert resp_patch.status_code == 403, (
                "Non-admin member should not be able to change roles."
            )

    async def test_member_permission_enforcement(
        self, authenticated_client_factory, user_owner, user_member_read, seed_users
    ):
        """Covers: READ member cannot add other members (POST)."""
        # 1. Setup KB as Owner
        async with authenticated_client_factory(user_owner) as owner_client:
            kb_data = await create_kb_helper(owner_client)
            kb_id = kb_data["_id"]
            # Add the read-only member
            await owner_client.post(
                f"/api/v1_ingestion/kb/{kb_id}/members",
                json={"user_id": str(user_member_read.id), "role": "read"},
            )

        # 2. Try to add a member acting as the Read-Only Member
        async with authenticated_client_factory(user_member_read) as member_client:
            resp = await member_client.post(
                f"/api/v1_ingestion/kb/{kb_id}/members",
                json={"user_id": "some-other-id", "role": "read"},
            )
            assert resp.status_code == 403

    async def test_member_revocation_blocks_access(
        self, authenticated_client_factory, user_owner, user_member_read, seed_users
    ):
        """Covers: Removing a member immediately blocks access to files and jobs."""
        # 1. Setup KB and Member
        async with authenticated_client_factory(user_owner) as owner_client:
            kb_data = await create_kb_helper(owner_client, "Revocation KB")
            kb_id = kb_data["_id"]
            await owner_client.post(
                f"/api/v1_ingestion/kb/{kb_id}/members",
                json={"user_id": str(user_member_read.id), "role": "read"},
            )

        # 2. Verify Access
        async with authenticated_client_factory(user_member_read) as member_client:
            resp = await member_client.get(f"/api/v1_ingestion/files/?scope=kb&kb_id={kb_id}")
            assert resp.status_code == 200

        # 3. Revoke Membership
        async with authenticated_client_factory(user_owner) as owner_client:
            resp = await owner_client.delete(
                f"/api/v1_ingestion/kb/{kb_id}/members/{user_member_read.id}"
            )
            assert resp.status_code == 204

        # 4. Verify Blocked
        async with authenticated_client_factory(user_member_read) as member_client:
            resp = await member_client.get(f"/api/v1_ingestion/files/?scope=kb&kb_id={kb_id}")
            assert resp.status_code == 403, "Revoked member should receive 403"

    async def test_ingestion_permission_enforcement(
        self,
        authenticated_client_factory,
        user_owner,
        user_member_read,
        user_member_write,
        mock_ingestion_deps,
        seed_users,
    ):
        """Covers: READ member cannot ingest, READ_WRITE member can."""
        # Setup KB
        async with authenticated_client_factory(user_owner) as owner_client:
            kb_data = await create_kb_helper(owner_client)
            kb_id = kb_data["_id"]
            # Add members
            await owner_client.post(
                f"/api/v1_ingestion/kb/{kb_id}/members",
                json={"user_id": str(user_member_read.id), "role": "read"},
            )
            await owner_client.post(
                f"/api/v1_ingestion/kb/{kb_id}/members",
                json={"user_id": str(user_member_write.id), "role": "read_write"},
            )

        # Case A: READ member tries to ingest -> 403
        async with authenticated_client_factory(user_member_read) as read_client:
            # We must use 'data' for UploadFile in TestClient, but httpx uses 'files'
            files = {"file": ("test.pdf", b"dummy content", "application/pdf")}
            # NOTE: We use the mounted test path
            # Settings.API_INGESTION is '/ingestion', so mounted path is /test-mount/ingestion/file/async
            resp = await read_client.post(
                f"/test-mount/ingestion/file/async?kb_id={kb_id}", files=files
            )
            assert resp.status_code == 403, (
                f"Read member should get 403, got {resp.status_code}: {resp.text}"
            )

        # Case B: READ_WRITE member tries to ingest -> 200
        async with authenticated_client_factory(user_member_write) as write_client:
            files = {"file": ("test.pdf", b"dummy content", "application/pdf")}
            resp = await write_client.post(
                f"/test-mount/ingestion/file/async?kb_id={kb_id}", files=files
            )
            assert resp.status_code == 200, (
                f"Write member should get 200, got {resp.status_code}: {resp.text}"
            )
            assert resp.json()["job_id"] is not None

    async def test_ingest_from_s3_url_permission_enforcement(
        self,
        authenticated_client_factory,
        user_owner,
        user_member_read,
        mock_ingestion_deps,
        seed_users,
    ):
        """Covers: READ member cannot ingest via URL endpoint (403)."""
        async with authenticated_client_factory(user_owner) as owner_client:
            kb_data = await create_kb_helper(owner_client, "S3 URL KB")
            kb_id = kb_data["_id"]
            await owner_client.post(
                f"/api/v1_ingestion/kb/{kb_id}/members",
                json={"user_id": str(user_member_read.id), "role": "read"},
            )

        async with authenticated_client_factory(user_member_read) as client:
            # Must mock the S3/Redis part, but RBAC happens first
            resp = await client.post(
                "/api/v1_ingestion/knowledge-base/ingest-from-s3-url",
                json={
                    "s3_url": "https://example.com/file.pdf",
                    "original_filename": "file.pdf",
                    "kb_id": kb_id,
                },
            )
            assert resp.status_code == 403

    async def test_files_list_scopes(
        self, authenticated_client_factory, user_owner, user_member_read, local_db, seed_users
    ):
        """
        Covers:
        - scope=personal (Personal Tenant)
        - scope=kb (Shared Inventory)
        - scope=all (My Uploads default)
        """
        # --- SEED DATA ---
        # Note: Must include ALL default fields to avoid validation errors when fetching
        base_job = {
            "status": "QUEUED",
            "weaviate_status": "QUEUED",
            "graphrag_status": "QUEUED",
            "created_at": "2025-01-01T00:00:00",
            "updated_at": "2025-01-01T00:00:00",
        }

        # 1. Job in Owner's Personal KB
        await local_db["IngestionJobs"].insert_one(
            {
                "_id": "job_personal",
                "user_id": str(user_owner.id),
                "tenant_id": str(user_owner.id),
                "original_filename": "personal.pdf",
                **base_job,
            }
        )

        # 2. Job in Shared KB (uploaded by Owner)
        kb_id = "kb_shared_123"
        await local_db["IngestionJobs"].insert_one(
            {
                "_id": "job_shared_owner",
                "user_id": str(user_owner.id),
                "kb_id": kb_id,
                "tenant_id": kb_id,
                "uploaded_by_user_id": str(user_owner.id),
                "original_filename": "shared_owner.pdf",
                **base_job,
            }
        )

        # 3. Job in Shared KB (uploaded by Member)
        await local_db["IngestionJobs"].insert_one(
            {
                "_id": "job_shared_member",
                "user_id": str(user_member_read.id),
                "kb_id": kb_id,
                "tenant_id": kb_id,
                "uploaded_by_user_id": str(user_member_read.id),
                "original_filename": "shared_member.pdf",
                **base_job,
            }
        )

        # Setup membership for Read User
        await local_db["KnowledgeBases"].insert_one(
            {
                "_id": kb_id,
                "tenant_id": kb_id,
                "owner_user_id": str(user_owner.id),
                "name": "Shared KB",
            }
        )
        await local_db["KnowledgeBaseMemberships"].insert_one(
            {"_id": "mem_1", "kb_id": kb_id, "user_id": str(user_member_read.id), "role": "read"}
        )

        # --- TEST OWNER VIEWS ---
        async with authenticated_client_factory(user_owner) as client:
            # A. scope=personal -> Only personal job
            resp = await client.get("/api/v1_ingestion/files/?scope=personal")
            assert resp.status_code == 200, resp.text
            files = resp.json()["files"]
            assert len(files) == 1
            assert files[0]["_id"] == "job_personal"

            # B. scope=all (Default) -> My uploads across all KBs
            # Should see 'job_personal' AND 'job_shared_owner'.
            # Should NOT see 'job_shared_member' (uploaded by someone else).
            resp = await client.get("/api/v1_ingestion/files/?scope=all")
            assert resp.status_code == 200, resp.text
            files = resp.json()["files"]
            ids = sorted([f["_id"] for f in files])
            assert ids == ["job_personal", "job_shared_owner"]

            # C. scope=kb -> Inventory of that KB (All uploaders)
            resp = await client.get(f"/api/v1_ingestion/files/?scope=kb&kb_id={kb_id}")
            assert resp.status_code == 200, resp.text
            files = resp.json()["files"]
            ids = sorted([f["_id"] for f in files])
            # Owner sees everything in the KB, regardless of who uploaded it
            assert ids == ["job_shared_member", "job_shared_owner"]

        # --- TEST MEMBER VIEWS ---
        async with authenticated_client_factory(user_member_read) as client:
            # D. scope=kb -> Inventory (Member has READ access)
            resp = await client.get(f"/api/v1_ingestion/files/?scope=kb&kb_id={kb_id}")
            assert resp.status_code == 200, resp.text
            files = resp.json()["files"]
            ids = sorted([f["_id"] for f in files])
            assert ids == ["job_shared_member", "job_shared_owner"]

            # E. scope=all -> Only Member's uploads
            resp = await client.get("/api/v1_ingestion/files/?scope=all")
            assert resp.status_code == 200, resp.text
            files = resp.json()["files"]
            assert len(files) == 1
            assert files[0]["_id"] == "job_shared_member"

    async def test_file_listing_invalid_params(self, authenticated_client_factory, user_owner):
        """Covers: Validation errors (400) for missing scope params or invalid enums."""
        async with authenticated_client_factory(user_owner) as client:
            # Missing kb_id when scope=kb
            resp = await client.get("/api/v1_ingestion/files/?scope=kb")
            assert resp.status_code == 400

            # Invalid Status Enum
            resp = await client.get("/api/v1_ingestion/files/?status=BOGUS_STATUS")
            assert resp.status_code == 400

    async def test_job_details_acl(
        self, authenticated_client_factory, user_owner, user_member_read, user_outsider, local_db
    ):
        """
        Covers: Accessing /jobs/{id}.
        - Owner: Yes
        - Member (READ): Yes (if job is in shared KB)
        - Outsider: No
        """
        kb_id = "kb_acl_test"
        # Job in Shared KB, uploaded by Owner
        job_id = "job_acl_1"

        # Manual insert with ALL fields to prevent validation errors
        await local_db["IngestionJobs"].insert_one(
            {
                "_id": job_id,
                "user_id": str(user_owner.id),
                "kb_id": kb_id,
                "tenant_id": kb_id,
                "uploaded_by_user_id": str(user_owner.id),
                "status": "QUEUED",
                "weaviate_status": "QUEUED",
                "graphrag_status": "QUEUED",
                "created_at": "2025-01-01T00:00:00",
                "updated_at": "2025-01-01T00:00:00",
            }
        )

        # Setup KB and Membership
        await local_db["KnowledgeBases"].insert_one(
            {
                "_id": kb_id,
                "tenant_id": kb_id,
                "owner_user_id": str(user_owner.id),
                "name": "Shared KB",
            }
        )
        await local_db["KnowledgeBaseMemberships"].insert_one(
            {"_id": "mem_1", "kb_id": kb_id, "user_id": str(user_member_read.id), "role": "read"}
        )

        # 1. Owner can access
        async with authenticated_client_factory(user_owner) as client:
            resp = await client.get(f"/api/v1_ingestion/jobs/{job_id}")
            assert resp.status_code == 200, resp.text

        # 2. Member (Read) can access
        async with authenticated_client_factory(user_member_read) as client:
            resp = await client.get(f"/api/v1_ingestion/jobs/{job_id}")
            assert resp.status_code == 200, resp.text

        # 3. Outsider cannot access
        async with authenticated_client_factory(user_outsider) as client:
            resp = await client.get(f"/api/v1_ingestion/jobs/{job_id}")
            assert resp.status_code == 403, (
                f"Outsider should get 403, got {resp.status_code}: {resp.text}"
            )

    async def test_search_endpoint_rbac(
        self, authenticated_client_factory, user_owner, user_outsider, local_db
    ):
        """Covers: Single KB search access control."""
        kb_id = "kb_search_auth"
        await local_db["KnowledgeBases"].insert_one(
            {
                "_id": kb_id,
                "tenant_id": kb_id,
                "owner_user_id": str(user_owner.id),
                "name": "Secret KB",
            }
        )

        # Patch the internal RAG logic to avoid hitting Weaviate/LLM
        with patch(
            "src.autobots.api.v1_ingestion.api_knowledge_base._get_rag_data", new_callable=AsyncMock
        ) as mock_rag:
            mock_rag.return_value = {"answer": "A", "sources": [], "graph_data": []}

            # Outsider -> 403
            async with authenticated_client_factory(user_outsider) as client:
                resp = await client.post(
                    "/api/v1_ingestion/knowledge-base/search",
                    json={"query": "x", "collection_name": "c", "kb_id": kb_id, "scope": "kb"},
                )
                assert resp.status_code == 403

            # Owner -> 200
            async with authenticated_client_factory(user_owner) as client:
                resp = await client.post(
                    "/api/v1_ingestion/knowledge-base/search",
                    json={"query": "x", "collection_name": "c", "kb_id": kb_id, "scope": "kb"},
                )
                assert resp.status_code == 200

    async def test_search_multi_kb_fanout_isolation(
        self, authenticated_client_factory, user_owner, local_db
    ):
        """
        Covers: Multi-KB (scope=all) search only queries allowed tenants.
        """
        # Setup: 3 KBs
        # 1. Personal (Implicit)
        # 2. Shared (Member)
        kb_shared = "kb_shared"
        await local_db["KnowledgeBases"].insert_one(
            {
                "_id": kb_shared,
                "tenant_id": "tenant_shared",
                "owner_user_id": "other",
                "name": "Shared",
            }
        )
        await local_db["KnowledgeBaseMemberships"].insert_one(
            {"kb_id": kb_shared, "user_id": str(user_owner.id), "role": "read"}
        )
        # 3. Inaccessible (Outsider)
        kb_forbidden = "kb_forbidden"
        await local_db["KnowledgeBases"].insert_one(
            {
                "_id": kb_forbidden,
                "tenant_id": "tenant_forbidden",
                "owner_user_id": "other",
                "name": "Forbidden",
            }
        )

        # --- Helpers for robust mocking ---
        class DummyChoice:
            def __init__(self, content):
                self.message = type("m", (), {"content": content})

        class DummyChatRes:
            def __init__(self, content):
                self.choices = [DummyChoice(content)]

        mock_chat_client = MagicMock()
        mock_chat_client.openai_chat.chat = AsyncMock(return_value=DummyChatRes("Summary"))

        with (
            patch(
                "src.autobots.api.v1_ingestion.api_knowledge_base._get_rag_data",
                new_callable=AsyncMock,
            ) as mock_rag,
            patch(
                "src.autobots.api.v1_ingestion.api_knowledge_base.IngestionLLMConnector.get_openai_client",
                new_callable=AsyncMock,
            ) as mock_llm,
        ):
            mock_rag.return_value = {"answer": "A", "sources": [], "graph_data": []}
            mock_llm.return_value = mock_chat_client

            async with authenticated_client_factory(user_owner) as client:
                resp = await client.post(
                    "/api/v1_ingestion/knowledge-base/search",
                    json={
                        "query": "test",
                        "collection_name": "c",
                        "scope": "all",
                        "vector_search_top_k": 1,
                    },
                )
                assert resp.status_code == 200

                # Assert logic:
                # Should call for Personal Tenant (user_owner.id) AND Shared Tenant ("tenant_shared")
                # Should NOT call for Forbidden Tenant ("tenant_forbidden")

                # Extract tenant_ids from kwargs
                called_tenants = {call.kwargs.get("tenant_id") for call in mock_rag.call_args_list}

                # Robust assertion: Required tenants must be present, forbidden must be absent
                expected_present = {str(user_owner.id), "tenant_shared"}
                assert expected_present.issubset(called_tenants), (
                    f"Missing expected tenants. Got: {called_tenants}"
                )
                assert "tenant_forbidden" not in called_tenants


@pytest.mark.asyncio
class TestRBACServiceLayer:
    """
    Unit tests for the logic in kb_service.py.
    """

    async def test_resolve_tenant_id_logic(
        self, local_db, user_owner, user_member_read, user_outsider
    ):
        # Setup KB and Membership directly in DB
        kb_id = "kb_logic_test"
        tenant_id = "tenant_logic_test"

        await local_db["KnowledgeBases"].insert_one(
            {
                "_id": kb_id,
                "tenant_id": tenant_id,
                "owner_user_id": str(user_owner.id),
                "name": "Logic KB",
            }
        )
        await local_db["KnowledgeBaseMemberships"].insert_one(
            {
                "_id": "mem_logic",
                "kb_id": kb_id,
                "user_id": str(user_member_read.id),
                "role": "read",
            }
        )

        # 1. Personal Scope (kb_id=None) -> returns user_id
        tid = await resolve_tenant_id_and_check_access(
            user_owner, local_db, kb_id=None, required_role=KnowledgeBaseRole.READ
        )
        assert tid == str(user_owner.id)

        # 2. Owner Access -> returns kb.tenant_id
        tid = await resolve_tenant_id_and_check_access(
            user_owner, local_db, kb_id=kb_id, required_role=KnowledgeBaseRole.ADMIN
        )
        assert tid == tenant_id

        # 3. Member Access (Sufficient Role) -> returns kb.tenant_id
        tid = await resolve_tenant_id_and_check_access(
            user_member_read, local_db, kb_id=kb_id, required_role=KnowledgeBaseRole.READ
        )
        assert tid == tenant_id

        # 4. Member Access (Insufficient Role) -> Raises 403
        with pytest.raises(HTTPException) as exc:
            await resolve_tenant_id_and_check_access(
                user_member_read, local_db, kb_id=kb_id, required_role=KnowledgeBaseRole.READ_WRITE
            )
        assert exc.value.status_code == 403

        # 5. Outsider Access -> Raises 403
        with pytest.raises(HTTPException) as exc:
            await resolve_tenant_id_and_check_access(
                user_outsider, local_db, kb_id=kb_id, required_role=KnowledgeBaseRole.READ
            )
        assert exc.value.status_code == 403

    async def test_kb_name_resolution_edge_cases(self, local_db, user_owner):
        """Covers: KB Name Resolution (Ambiguous / Not Found)."""
        # 1. Setup Ambiguous Names
        await local_db["KnowledgeBases"].insert_one(
            {
                "_id": "kb_1",
                "name": "DupName",
                "owner_user_id": str(user_owner.id),
                "tenant_id": "t1",
            }
        )
        # Mock user is member of another KB with same name
        await local_db["KnowledgeBases"].insert_one(
            {"_id": "kb_2", "name": "DupName", "owner_user_id": "other", "tenant_id": "t2"}
        )
        await local_db["KnowledgeBaseMemberships"].insert_one(
            {"kb_id": "kb_2", "user_id": str(user_owner.id), "role": "read"}
        )

        # Test Ambiguous (400)
        with pytest.raises(HTTPException) as exc:
            await resolve_kb_id_by_name_for_user(local_db, user_owner, "DupName")
        assert exc.value.status_code == 400

        # Test Not Found (404)
        with pytest.raises(HTTPException) as exc:
            await resolve_kb_id_by_name_for_user(local_db, user_owner, "NonExistent")
        assert exc.value.status_code == 404

    async def test_kb_list_enrichment(
        self, authenticated_client_factory, user_owner, user_member_read, local_db, seed_users
    ):
        """Covers: GET /kb/ returns correct file_count, member_count, and role."""
        kb_id = "kb_enrich_test"
        tenant_id = "tenant_enrich_test"

        # 1. Setup KB and Member
        await local_db["KnowledgeBases"].insert_one(
            {
                "_id": kb_id,
                "tenant_id": tenant_id,
                "owner_user_id": str(user_owner.id),
                "name": "Stats KB",
            }
        )
        await local_db["KnowledgeBaseMemberships"].insert_one(
            {"kb_id": kb_id, "user_id": str(user_member_read.id), "role": "read"}
        )

        # 2. Add 3 Files (2 Active, 1 Failed)
        # ENHANCED: Use more complete job docs for stability
        base_job = {
            "user_id": str(user_owner.id),
            "tenant_id": tenant_id,
            "kb_id": kb_id,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "weaviate_status": "AVAILABLE",
            "graphrag_status": "AVAILABLE",
        }
        await local_db["IngestionJobs"].insert_many(
            [
                {"_id": "j1", "status": "AVAILABLE", **base_job},
                {
                    "_id": "j2",
                    "status": "PROCESSING",
                    **base_job,
                },  # Included in ACTIVE_FILE_STATUSES
                {"_id": "j3", "status": "FAILED", **base_job},  # Excluded
            ]
        )

        # 3. Test as Owner
        async with authenticated_client_factory(user_owner) as client:
            resp = await client.get("/api/v1_ingestion/kb/")
            assert resp.status_code == 200, resp.text
            kbs = resp.json()
            target_kb = next(k for k in kbs if k["_id"] == kb_id)
            assert target_kb["file_count"] == 2  # AVAILABLE + PROCESSING
            assert target_kb["member_count"] == 2  # Owner + Member
            assert target_kb["current_user_role"] == "admin"

        # 4. Test as Member (Verify role and stats are visible)
        async with authenticated_client_factory(user_member_read) as client:
            resp = await client.get("/api/v1_ingestion/kb/")
            assert resp.status_code == 200, resp.text
            kbs = resp.json()
            target_kb = next(k for k in kbs if k["_id"] == kb_id)

            assert target_kb["file_count"] == 2
            assert target_kb["member_count"] == 2
            assert target_kb["current_user_role"] == "read"

    async def test_kb_crud_operations(
        self, authenticated_client_factory, user_owner, user_member_read, local_db, seed_users
    ):
        """Covers: PATCH (update) and DELETE endpoints."""
        # 1. Create KB as Owner
        async with authenticated_client_factory(user_owner) as client:
            kb_data = await create_kb_helper(client, "CRUD KB")
            kb_id = kb_data["_id"]

            # 2. PATCH (Owner)
            resp = await client.patch(
                f"/api/v1_ingestion/kb/{kb_id}", json={"name": "Updated Name"}
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["name"] == "Updated Name"

        # 3. PATCH (Member - Forbidden)
        await local_db["KnowledgeBaseMemberships"].insert_one(
            {"kb_id": kb_id, "user_id": str(user_member_read.id), "role": "read"}
        )
        async with authenticated_client_factory(user_member_read) as client:
            resp = await client.patch(f"/api/v1_ingestion/kb/{kb_id}", json={"name": "Hacked Name"})
            assert resp.status_code == 403

        # 4. DELETE (Owner - Success)
        async with authenticated_client_factory(user_owner) as client:
            resp = await client.delete(f"/api/v1_ingestion/kb/{kb_id}")
            assert resp.status_code == 204

            # Verify it's gone
            get_resp = await client.get(f"/api/v1_ingestion/kb/{kb_id}")
            assert get_resp.status_code == 404

            # ENHANCED: Verify memberships are deleted
            mem_count = await local_db["KnowledgeBaseMemberships"].count_documents({"kb_id": kb_id})
            assert mem_count == 0
