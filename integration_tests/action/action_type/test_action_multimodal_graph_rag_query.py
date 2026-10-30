# tests/action/action_type/test_action_multimodal_graph_rag_query.py

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import httpx  # Import httpx for the new helper
import pytest
import pytest_asyncio
from jose import jwt
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorClient
from weaviate.classes.tenants import Tenant
from weaviate.exceptions import WeaviateBaseError

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_multimodal.action_multimodal_graph_rag_query import (
    ActionGraphRAGQuery,
    GraphRAGQueryConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.aws.aws_s3 import get_aws_s3
from src.autobots.conn.weaviate.weaviate_client import WeaviateVectorDBClient
from src.autobots.core.redis.async_pool import get_async_redis
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.ingestion.job_status import JobState
from src.autobots.ingestion.neo4j_loader import Neo4jManager
from src.autobots.ingestion.polling import poll_for_job_completion
from src.autobots.user.user_orm_model import UserORM

# -----------------------------------------------------------------
# 0.  remove any jobs that a previous nightly-sync left in Redis
#     (run once for this module, before the first test)
# -----------------------------------------------------------------


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _module_setup_and_cleanup():
    """
    A module-level fixture to ensure a clean state before tests run.
    1. Purges Redis queues of any stale jobs.
    2. Deletes known "noisy" files from S3 that the cron job would otherwise pick up.
    """
    settings = SettingsProvider.sget()

    # 1. Purge Redis Queues
    try:
        redis = await get_async_redis()
    except RuntimeError:
        logger.warning("Redis not available (no app lifespan) — skipping queue purge")
    else:
        high_q = settings.REDIS_INGESTION_HIGH_PRIORITY_QUEUE
        low_q = settings.REDIS_INGESTION_LOW_PRIORITY_QUEUE
        await redis.delete(high_q)
        await redis.delete(low_q)

    # 1b. Purge stale test/sync jobs from MongoDB to ensure a clean slate
    db_client = AsyncIOMotorClient(settings.MONGO_CONN)
    db = db_client[settings.MONGO_DATABASE]
    # Delete jobs from the hardcoded sync user AND the fallback test user from deps.py
    await db["IngestionJobs"].delete_many(
        {
            "$or": [
                {"user_id": "e29f3392-6124-40f5-8e24-fd6b5ebff875"},
                {"user_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"},
            ]
        }
    )
    db_client.close()

    # 2. Delete the offending S3 prefix that contains blank.pdf and empty.pdf
    # s3 = get_aws_s3(
    #     settings.AWS_S3_BUCKET_REGION,
    #     settings.AWS_ACCESS_KEY_ID,
    #     settings.AWS_SECRET_ACCESS_KEY,
    #     settings.AWS_S3_PUBLIC_BUCKET_NAME,
    #     object_prefix="",
    # )
    # offending_prefix = "files/e29f3392-6124-40f5-8e24-fd6b5ebff875/"
    # await s3.delete_prefix(offending_prefix)

    yield  # run the tests


# -----------------------------------------------------------------
# 1. Wait until the worker’s /health endpoint is up
# -----------------------------------------------------------------

WORKER_BASE = os.getenv("WORKER_BASE_URL", "http://worker:8002")


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _wait_for_worker():
    for _ in range(90):  # max 90 s
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{WORKER_BASE}/health", timeout=3)
                if r.status_code == 200:
                    return
        except Exception:
            pass
        await asyncio.sleep(1)
    pytest.exit("ingestion-worker never became healthy", 1)


# -----------------------------------------------------------------
# 2. Helper to HEAD-probe the uploaded file until it is really public
# -----------------------------------------------------------------
async def _wait_until_public(url: str, timeout: int = 30):
    """
    Polls a public URL with HEAD requests until it returns 200 OK,
    ensuring an object is publicly readable before proceeding.
    """
    async with httpx.AsyncClient() as c:
        for _ in range(timeout):
            try:
                r = await c.head(url, timeout=5.0)
                if r.status_code == 200:
                    return
            except httpx.RequestError:
                pass
            await asyncio.sleep(1)
    raise RuntimeError(f"{url} never became publicly readable in {timeout}s")


# -----------------------------------------------------------------
# 3.  Additional Test Helpers
# -----------------------------------------------------------------


async def download_and_upload_media(ctx: Context, asset: dict, s3_client, temp_dir: Path) -> str:
    """
    Downloads a media file from a URL and uploads it to the test S3 bucket.
    """
    dest_path = temp_dir / asset["filename"]
    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        try:
            response = await client.get(asset["url"])
            response.raise_for_status()
            dest_path.write_bytes(response.content)
        except httpx.ConnectError as e:
            pytest.fail(
                f"Failed to connect to download media asset at {asset['url']}. "
                f"Ensure you are connected to the internet/VPN. Error: {e}"
            )

    with open(dest_path, "rb") as f:
        s3_url = await s3_client.upload_fileobj(ctx, f, dest_path.name)
        logger.success(f"Uploaded '{asset['filename']}' to S3: {s3_url}")
        return str(s3_url)


def assert_concepts_covered(answer: str, concepts: dict):
    """
    Asserts that the RAG answer contains keywords for each required concept.
    """
    answer_lower = answer.lower()
    for concept, keywords in concepts.items():
        is_covered = any(keyword.lower() in answer_lower for keyword in keywords)
        assert is_covered, (
            f"Assertion Failed: Did not cover concept '{concept}' (keywords: {keywords}). Answer: {answer}"
        )
    logger.success("Assertion Passed: All required concepts were correctly covered.")


# --- Test Configuration ---
settings = SettingsProvider.sget()

# --- Test Behavior Toggle ---
# This mirrors the feature toggle in the Action itself. Set to False to test the
# new default behavior (separate ingest/query). Set to True to test the
# legacy combined ingest-then-query flow. NOTE: The Action's own toggle must
# also be flipped for the `True` path to pass.
TEST_COMBINED_INGEST_THEN_QUERY = False

pytestmark = pytest.mark.skipif(
    (settings.ENV != "local")
    or (
        not all(
            [
                settings.WEAVIATE_URL,
                settings.WEAVIATE_API_KEY,
                settings.NEO4J_URI,
                settings.NEO4J_USERNAME,
                settings.NEO4J_PASSWORD,
                settings.OPENAI_API_KEY,
                settings.AWS_S3_PUBLIC_BUCKET_NAME,
            ]
        )
    ),
    reason="This is an E2E test that requires ENV=local and all dependent service secrets.",
)


@pytest_asyncio.fixture(scope="module")
async def neo4j_client_module():
    """
    A module-scoped fixture to manage the Neo4j client singleton.
    """
    # Initializing the client is now done here, once per module.
    client = await Neo4jManager.get_client()
    yield client
    # The shutdown call is now correctly scoped to the module.
    Neo4jManager.shutdown()


@pytest_asyncio.fixture(scope="function")
async def test_env(neo4j_client_module):
    """
    Sets up a clean environment for a SINGLE test function by creating a unique user,
    S3 prefix, and temp directory. It performs surgical cleanup of DATA ONLY, leaving
    the module-scoped connections intact.
    """
    user_id = str(uuid.uuid4())
    user = UserORM(id=user_id)
    # The action is designed to work with the user's permanent knowledge base.
    collection_name = "synced_files"
    temp_dir = Path(tempfile.mkdtemp())
    # Use a unique S3 prefix that matches the worker's expected staging pattern.
    s3_prefix = f"files/{user_id}/"

    s3_client = get_aws_s3(
        settings.AWS_S3_BUCKET_REGION,
        settings.AWS_ACCESS_KEY_ID,
        settings.AWS_SECRET_ACCESS_KEY,
        settings.AWS_S3_PUBLIC_BUCKET_NAME,
        object_prefix=s3_prefix,
    )

    # --- SETUP: PRE-CREATE AND VERIFY WEAVIATE TENANT ---
    # This is the key fix to prevent the 'tenant not found' race condition
    # that only occurs in our high-speed test environment.
    weaviate_client = None
    try:
        logger.info(f"--- Pre-creating and verifying tenant '{user_id}' for test ---")
        weaviate_client = WeaviateVectorDBClient(
            url=settings.WEAVIATE_URL, api_key=settings.WEAVIATE_API_KEY, skip_init_checks=True
        )
        collection = weaviate_client.client.collections.get(name=collection_name)
        if not await asyncio.to_thread(collection.tenants.exists, user_id):
            await asyncio.to_thread(collection.tenants.create, [Tenant(name=user_id)])

        # Poll for up to 30 seconds to ensure the tenant is fully writable.
        # A simple read-probe is not enough; we must also do a write-probe.
        for attempt in range(30):
            try:
                # 1. Read Probe (checks if metadata is propagated)
                await asyncio.to_thread(
                    collection.with_tenant(user_id).aggregate.over_all, total_count=True
                )
                # 2. Write Probe (checks if the data shard is active)
                dummy_uuid = str(uuid.uuid4())
                await asyncio.to_thread(
                    collection.with_tenant(user_id).data.insert,
                    properties={"page_content": "activation_probe"},
                    uuid=dummy_uuid,
                )
                # 3. Cleanup Probe
                await asyncio.to_thread(
                    collection.with_tenant(user_id).data.delete_by_id, uuid=dummy_uuid
                )

                logger.success(
                    f"Tenant '{user_id}' shard is active and writable after {attempt + 1}s."
                )
                break  # Success!
            except WeaviateBaseError as e:
                if "tenant not found" in str(e).lower():
                    logger.debug(
                        f"Tenant activation check failed on attempt {attempt + 1}. Retrying..."
                    )
                    await asyncio.sleep(1)
                else:
                    pytest.fail(f"Tenant activation failed with unexpected Weaviate error: {e}")
        else:
            pytest.fail(f"Tenant '{user_id}' did not become writable after 30 seconds.")

    finally:
        if weaviate_client:
            await weaviate_client.close()

    yield user, collection_name, s3_client, temp_dir

    # --- TEARDOWN: FUNCTION-SCOPED CLEANUP (DATA ONLY) ---
    logger.info(f"--- Cleaning up data for test with user {user_id} ---")
    try:
        # 1. S3 and Local File Cleanup
        await s3_client.delete_prefix(s3_prefix)
        if temp_dir.exists():
            shutil.rmtree(temp_dir)

        # 2. Weaviate Cleanup
        # We don't delete the collection, but we must remove the tenant.
        weaviate_client = WeaviateVectorDBClient(
            url=settings.WEAVIATE_URL, api_key=settings.WEAVIATE_API_KEY, skip_init_checks=True
        )
        collection = weaviate_client.client.collections.get(name=collection_name)
        if await asyncio.to_thread(collection.tenants.exists, user_id):
            await asyncio.to_thread(collection.tenants.remove, [user_id])
        await weaviate_client.close()

        # 3. Neo4j Cleanup (delete tenant data, DO NOT shut down the client)
        await asyncio.to_thread(neo4j_client_module.delete_all_tenant_data, tenant_id=user_id)

        # 4. MongoDB IngestionJobs Cleanup
        mongo_client = AsyncIOMotorClient(settings.MONGO_CONN)
        db = mongo_client[settings.MONGO_DATABASE]
        # Use the user_id from the test run to scope the deletion
        result = await db["IngestionJobs"].delete_many({"user_id": user_id})
        mongo_client.close()
        logger.info(f"Cleaned up {result.deleted_count} IngestionJobs for user {user_id}")

    except Exception as e:
        logger.error(f"An error occurred during cleanup: {e}")


@pytest_asyncio.fixture(scope="function")
async def db_session():
    """
    Provides a correctly-scoped, test-specific async database session
    that is guaranteed to be attached to the correct event loop.
    """
    settings = SettingsProvider.sget()  # Get settings
    client = AsyncIOMotorClient(settings.MONGO_CONN)
    _db = client[settings.MONGO_DATABASE]
    try:
        yield _db
    finally:
        client.close()  # Clean up the test-specific client


async def run_action(user, collection_name, action_input_dict, db_session):
    """Helper function to reduce boilerplate in test cases."""
    app_state = AppState()
    expire = datetime.utcnow() + timedelta(minutes=5)
    to_encode = {"sub": str(user.id), "exp": int(expire.timestamp())}
    mock_token = jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    user.access_token = mock_token
    ctx = Context(user_id=str(user.id))

    action_config = GraphRAGQueryConfig(
        query=action_input_dict.get("text", ""), collection_name=collection_name
    )
    action_doc = ActionDoc(
        _id=str(uuid.uuid4()),
        name=f"Test Action for {collection_name}",
        type=ActionType.multimodal_graph_rag_query,
        config=action_config.model_dump(),
        user_id=str(user.id),
    )

    return await ActionFactory.run_action(
        ctx, app_state, action_doc, action_input_dict, user=user, db=db_session
    )


@pytest.mark.asyncio
@pytest.mark.skip("Crashing the system")
async def test_graphrag_action_intent_ingest_and_query(
    ctx, test_env, set_test_settings, db_session
):
    """
    Tests the "Ingest-and-Query" intent end-to-end. This is the primary test for the
    stabilization fix, verifying the thin-client HTTP calls and temporary wait work together.
    """
    user, collection_name, s3_client, temp_dir = test_env

    # --- FIX: Create a unique filename for this test run ---
    unique_suffix = uuid.uuid4().hex[:8]
    base_filename = "project_orion_briefing"
    unique_filename = f"{base_filename}_{unique_suffix}.txt"

    # ARRANGE: Upload a file to S3 to get a public URL for the action to use.
    local_file = temp_dir / unique_filename
    local_file.write_text(
        "Project Orion is a Q3 initiative focused on renewable energy. The lead engineer is Dr. Aris Thorne. Financial allocation is five million dollars."
    )
    with open(local_file, "rb") as f:
        s3_url = str(await s3_client.upload_fileobj(ctx, f, local_file.name))

    # --- SOLUTION: Use the clean S3 URL for processing, not the presigned one ---
    # The presigned URL is only for temporary, authenticated access by the worker.
    # The canonical identifier for the file should be the simple HTTPS URL.
    await _wait_until_public(s3_url)  # Wait for S3 eventual consistency

    action_input = DataBlock(
        urls=[s3_url], text="Who is the lead engineer for Project Orion?"
    ).model_dump()

    # We now rely entirely on `poll_for_job_completion` to correctly wait for the
    # background worker to finish processing and for the data to become available.

    # ACT
    result = await run_action(user, collection_name, action_input, db_session)

    # ASSERT: The assertions now depend on whether the combined flow is active.
    assert hasattr(result, "output_dict"), "Result should have an output_dict"
    response_blocks = result.output_dict.get("blocks", [])
    assert len(response_blocks) == 1, "Expected exactly one DataBlock in the response"

    if TEST_COMBINED_INGEST_THEN_QUERY:
        # --- ASSERT (Legacy Behavior): Check for a full RAG response ---
        single_block = response_blocks[0]

        # 1. Validate the primary text answer
        answer = single_block["text"].lower()
        assert len(answer) > 10, "Should be a substantial response"
        # Verify it actually answered about the content
        assert "aris thorne" in answer and "orion" in answer

        # 2. Validate the contents of the search_data wrapper object
        search_data = single_block.get("search_data")
        assert search_data is not None, "A search_data object was not found in the response"

        sources = search_data.get("sources", [])
        assert len(sources) > 0, "Source list should not be empty"

        first_source = sources[0]
        assert "chunk_id" in first_source and first_source["chunk_id"] is not None
        assert "document_id" in first_source and first_source.get("document_id") is not None
        assert "source" in first_source and first_source["source"] == unique_filename
        assert "score" in first_source and isinstance(first_source["score"], float)

        graph_payload = search_data.get("graph_visualization")
        assert graph_payload is not None, "A graph_visualization payload was not found"
        assert "nodes" in graph_payload and len(graph_payload["nodes"]) > 0
        assert "edges" in graph_payload  # Edges can be empty for a simple graph

        # 3. Verify data exists in both backends
        # Check Weaviate
        weaviate_client = WeaviateVectorDBClient(
            url=settings.WEAVIATE_URL, api_key=settings.WEAVIATE_API_KEY, skip_init_checks=True
        )
        collection = weaviate_client.client.collections.get(name=collection_name)
        agg_result = await asyncio.to_thread(
            collection.with_tenant(str(user.id)).aggregate.over_all, total_count=True
        )
        await weaviate_client.close()
        assert agg_result.total_count > 0, "Data was not found in Weaviate for the correct tenant."

        # Check Neo4j
        neo4j_client = await Neo4jManager.get_client()
        neo4j_result = await asyncio.to_thread(
            neo4j_client.query,
            f"MATCH (n:Chunk {{tenantId: '{user.id}'}}) RETURN count(n) as count",
            str(user.id),
        )
        assert neo4j_result[0]["count"] > 0, "Data was not found in Neo4j for the correct tenant."

    else:
        # --- ASSERT (New Default Behavior): Check for an instructional message ---
        single_block = response_blocks[0]

        # 1. Assert the instructional message is present
        answer_text = single_block["text"]
        assert "Successfully queued" in answer_text
        assert "Please ask your question again in a new message" in answer_text

        # 2. Assert that NO search was performed
        assert single_block.get("search_data") is None

        # 3. Assert that a job was actually created in the database
        job_doc = await db_session["IngestionJobs"].find_one(
            {"user_id": str(user.id), "original_filename": unique_filename}
        )
        assert job_doc is not None, "An ingestion job should have been created in MongoDB."


@pytest.mark.asyncio
@pytest.mark.skip("Crashing the system")
async def test_graphrag_action_intent_ingest_only_and_query_only(
    ctx, test_env, set_test_settings, db_session
):
    """
    Tests the "Ingest Only" and "Query Only" intents as a realistic user workflow.
    """
    user, collection_name, s3_client, temp_dir = test_env

    # --- FIX: Create a unique filename for this test run ---
    unique_suffix = uuid.uuid4().hex[:8]
    base_filename = "voyager_log_entry_7"
    unique_filename = f"{base_filename}_{unique_suffix}.md"

    # ARRANGE: Upload a file to S3.
    local_file = temp_dir / unique_filename
    local_file.write_text(
        "Log Entry 7: The Voyager probe has discovered an anomaly near the Kepler-186f system. The anomaly is emitting a repeating signal, designated Signal-Zeta."
    )
    with open(local_file, "rb") as f:
        s3_url = str(await s3_client.upload_fileobj(ctx, f, local_file.name))

    # --- SOLUTION: Use the clean S3 URL for processing, not the presigned one ---
    await _wait_until_public(s3_url)

    # --- ACT 1: Test "Ingest Only" Intent ---
    # This input has a URL but no query text.
    ingest_input = DataBlock(urls=[s3_url]).model_dump()
    ingest_result = await run_action(user, collection_name, ingest_input, db_session)

    # ASSERT 1: Check immediate response
    assert "Successfully queued" in ingest_result.output_dict["blocks"][0]["text"]

    # Use proper job tracking instead of hardcoded sleep
    # Find the job by filename for robust testing
    job_doc = await db_session["IngestionJobs"].find_one(
        {"user_id": str(user.id), "original_filename": unique_filename},
        sort=[("created_at", -1)],  # Get the most recent one
    )
    # The only thing we need to know at this point is that the job was created.
    # Its state could be QUEUED, PROCESSING, or even AVAILABLE if it was very fast.
    assert job_doc, "The ingestion job document was not created in the database."

    job_id = job_doc["_id"]

    # Use proper polling instead of hardcoded sleep
    # This workflow requires the graph, so we must wait for full availability.
    final_job_states = [JobState.AVAILABLE, JobState.FAILED]
    final_status = await poll_for_job_completion(
        job_id, db=db_session, timeout_seconds=120, final_states=final_job_states
    )

    # --- DETECTIVE WORK: Dump recent jobs on failure for analysis ---
    if final_status.get("status") == JobState.FAILED.value:
        print("\n\n---- DETECTIVE WORK: DUMPING RECENT JOBS (test_ingest_only_and_query_only) ----")
        cursor = db_session["IngestionJobs"].find({}).sort("created_at", -1).limit(10)
        async for doc in cursor:
            print(
                f"  - JobID: {doc.get('_id')}, UserID: {doc.get('user_id', 'N/A')[:8]}..., "
                f"File: {doc.get('original_filename', 'N/A')}, Status: {doc.get('status', 'N/A')}, "
                f"Error: {doc.get('error_message', 'None')}"
            )
        print("---------------------------------------------------------------------------\n")

    assert final_status["status"] == JobState.AVAILABLE.value, (
        f"Job failed with status: {final_status.get('status')} and error: {final_status.get('error_message')}"
    )

    # --- ACT 2: Test "Query Only" Intent ---
    # This input has a query but no URL.
    query_input = DataBlock(
        text="What was the designation for the anomalous signal near Kepler-186f?"
    ).model_dump()
    query_result = await run_action(user, collection_name, query_input, db_session)

    # --- VERIFICATION PRINT #3: Show the final, complete RAG response ---
    print("\n" + "=" * 80)
    print("✅ VERIFICATION #3: Final RAG result from 'Query Only' action.")
    print("   -> Check 'search_data.sources[0].metadata' for the 's3_url' field.")
    print(json.dumps(query_result.output_dict, indent=2, default=str))
    print("=" * 80 + "\n")

    # --- ENHANCED ASSERTIONS for ACT 2 ---
    assert hasattr(query_result, "output_dict"), "Query result should have an output_dict"

    # 1. Validate the overall structure
    response_blocks = query_result.output_dict.get("blocks", [])
    assert len(response_blocks) == 1, "Expected exactly one DataBlock for a query-only call"
    single_block = response_blocks[0]

    # 2. Validate the primary text answer
    answer = single_block["text"].lower().replace("-", " ")  # Normalize for LLM variations
    assert "signal zeta" in answer, f"Expected 'signal zeta' in answer: {answer}"

    # 3. Validate the contents of the search_data wrapper object
    search_data = single_block.get("search_data")
    assert search_data is not None, "A search_data object was not found in the query-only response"

    sources = search_data.get("sources", [])
    assert len(sources) > 0, "Source list should not be empty for a successful query"
    assert unique_filename in sources[0]["source"]

    # Verify s3_url is present and valid
    first_source = sources[0]
    assert "s3_url" in first_source, "s3_url key should be in the source metadata"
    assert first_source["s3_url"] is not None, "s3_url value should not be None"
    assert first_source["s3_url"].startswith("https://"), "s3_url should be a valid HTTPS URL"
    assert unique_filename in first_source["s3_url"], "s3_url should contain the filename"

    graph_payload = search_data.get("graph_visualization")
    assert graph_payload is not None, (
        "A graph_visualization payload was not found in the query-only response"
    )
    assert "nodes" in graph_payload and len(graph_payload["nodes"]) > 0
    assert any("signal-zeta" in node.get("label", "").lower() for node in graph_payload["nodes"])


@pytest.mark.asyncio
@pytest.mark.skip("Crashing system")
async def test_graphrag_action_run_tool(ctx, test_env, set_test_settings, db_session):
    """
    Tests the `run_tool` method directly, simulating a call from the ToolFactory.
    """
    user, collection_name, s3_client, temp_dir = test_env

    # --- FIX: Create a unique filename for this test run ---
    unique_suffix = uuid.uuid4().hex[:8]
    base_filename = "security_directive_alpha"
    unique_filename = f"{base_filename}_{unique_suffix}.txt"

    # ARRANGE: Pre-seed data using the action's "Just Ingest" intent
    local_file = temp_dir / unique_filename
    local_file.write_text(
        "Security Directive Alpha: In case of a containment breach, all personnel must use the emergency evacuation key 'Crimson-Echo-7'."
    )
    with open(local_file, "rb") as f:
        s3_url = str(await s3_client.upload_fileobj(ctx, f, local_file.name))

    # --- SOLUTION: Use the clean S3 URL for processing, not the presigned one ---
    await _wait_until_public(s3_url)

    ingest_input = DataBlock(urls=[s3_url]).model_dump()
    await run_action(user, collection_name, ingest_input, db_session)

    # Use proper polling instead of hardcoded sleep
    # Find and wait for the job to complete
    job_doc = await db_session["IngestionJobs"].find_one(
        {"user_id": str(user.id), "original_filename": unique_filename},
        sort=[("created_at", -1)],
    )
    assert job_doc, "Pre-seeding job was not created"

    # This workflow requires the graph, so we must wait for full availability.
    final_status = await poll_for_job_completion(
        job_doc["_id"],
        db=db_session,
        timeout_seconds=120,
        final_states=[JobState.AVAILABLE, JobState.FAILED],
    )

    # --- DETECTIVE WORK: Dump recent jobs on failure for analysis ---
    if final_status.get("status") == JobState.FAILED.value:
        print("\n\n---- DETECTIVE WORK: DUMPING RECENT JOBS (test_run_tool) ----")
        cursor = db_session["IngestionJobs"].find({}).sort("created_at", -1).limit(10)
        async for doc in cursor:
            print(
                f"  - JobID: {doc.get('_id')}, UserID: {doc.get('user_id', 'N/A')[:8]}..., "
                f"File: {doc.get('original_filename', 'N/A')}, Status: {doc.get('status', 'N/A')}, "
                f"Error: {doc.get('error_message', 'None')}"
            )
        print("-------------------------------------------------------------------\n")

    assert final_status["status"] == JobState.AVAILABLE.value, (
        f"Pre-seeding job failed with status: {final_status.get('status')} and error: {final_status.get('error_message')}"
    )

    # This config would be generated by an LLM
    tool_config = GraphRAGQueryConfig(
        query="What is the evacuation key from Security Directive Alpha?",
        collection_name=collection_name,
    )

    # Create the action instance, which is what the ToolFactory does internally
    # db is injected to follow the correct dependency injection pattern for efficiency and consistency.
    action_instance = ActionGraphRAGQuery(action_config=tool_config, user=user, db=db_session)

    # Create a mock context with a valid token
    expire = datetime.utcnow() + timedelta(minutes=5)
    to_encode = {"sub": str(user.id), "exp": int(expire.timestamp())}
    mock_token = jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    user.access_token = mock_token
    ctx = Context(user_id=str(user.id))

    # ACT
    app_state = AppState()
    result = await action_instance.run_tool(action_config=tool_config, ctx=ctx, app_state=app_state)

    # ASSERT
    assert isinstance(result, DataBlocks)
    assert len(result.blocks) == 1, "run_tool should return exactly one DataBlock"
    single_block = result.blocks[0]

    answer = single_block.text.lower().replace("-", " ")  # Normalize
    assert "crimson echo 7" in answer, f"Expected 'crimson echo 7' in answer: {answer}"

    assert single_block.search_data is not None, (
        "search_data should be present in a successful tool run"
    )
    assert len(single_block.search_data.sources) > 0, "Sources should be present in search_data"

    # Verify s3_url and order property in the results
    first_source = single_block.search_data.sources[0]
    # FIX: first_source is a Pydantic model, not a dict. Use attribute access.
    assert hasattr(first_source, "s3_url"), "s3_url attribute should exist on SourceChunk"
    assert first_source.s3_url is not None, "s3_url value should not be None"
    assert first_source.s3_url.startswith("https://"), "s3_url should be a valid HTTPS URL"
    assert unique_filename in first_source.s3_url, "s3_url should contain the filename"

    graph_viz = single_block.search_data.graph_visualization
    assert graph_viz and "nodes" in graph_viz
    chunk_node = next((n for n in graph_viz["nodes"] if n.get("type") == "Chunk"), None)
    assert chunk_node is not None, "A chunk node should be present in the graph visualization"
    assert "order" in chunk_node["details"]["properties"], (
        "The 'order' property should exist in the chunk node properties"
    )
    assert chunk_node["details"]["properties"]["order"] == 0


@pytest.mark.asyncio
@pytest.mark.skip("Crashing the system")
async def test_graphrag_multi_chunk_document_ordering(ctx, test_env, set_test_settings, db_session):
    """
    Tests that a multi-chunk document is correctly ordered in the RAG response.
    This test verifies:
    1. Multiple chunks are created from a longer document.
    2. Each chunk has an 'order' property.
    3. Chunks are returned in the correct order in the final RAG context.
    4. The LLM's answer reflects the correct chronological flow of the document.
    """
    user, collection_name, s3_client, temp_dir = test_env

    # --- ARRANGE: Create a multi-section document that will be split into chunks ---
    unique_suffix = uuid.uuid4().hex[:8]
    base_filename = "mars_mission_report"
    unique_filename = f"{base_filename}_{unique_suffix}.txt"

    document_content = """
Section 1: Launch Sequence
The Ares-7 spacecraft successfully launched from Kennedy Space Center. All primary systems nominal.
Crew complement: Commander Sarah Chen, Pilot Marcus Rodriguez.

Section 2: Transit Phase
Day 45 of transit. All crew members in good health. Hydroponics bay producing fresh vegetables.
Communications delay now 12 minutes each way.

Section 3: Mars Orbit Insertion
Successfully entered Mars orbit on day 187. Orbital parameters are nominal.
Initial surveys of landing site show favorable conditions.

Section 4: Landing Operations
EDL (Entry, Descent, Landing) sequence initiated. Heat shield performed nominally.
Touchdown confirmed at coordinates 18.4°N, 77.5°E. All systems green.

Section 5: Initial Findings
First EVA completed successfully. Soil samples collected. Water ice detected 30cm below surface.
Dr. Tanaka reports: "This confirms our models."
""".strip()

    local_file = temp_dir / unique_filename
    local_file.write_text(document_content)
    with open(local_file, "rb") as f:
        s3_url = str(await s3_client.upload_fileobj(ctx, f, local_file.name))

    await _wait_until_public(s3_url)

    # --- ACT 1: Ingest the document ---
    ingest_input = DataBlock(urls=[s3_url]).model_dump()
    await run_action(user, collection_name, ingest_input, db_session)

    job_doc = await db_session["IngestionJobs"].find_one(
        {"user_id": str(user.id), "original_filename": unique_filename},
        sort=[("created_at", -1)],
    )
    assert job_doc, "Ingestion job was not created"

    final_status = await poll_for_job_completion(
        job_doc["_id"],
        db=db_session,
        timeout_seconds=180,  # Increased timeout for longer document
        final_states=[JobState.AVAILABLE, JobState.FAILED],
    )
    assert final_status["status"] == JobState.AVAILABLE.value, (
        f"Ingestion failed: {final_status.get('error_message')}"
    )

    # --- ACT 2: Query to retrieve the full document context ---
    query_input = DataBlock(text="Summarize the Mars mission from launch to landing.").model_dump()
    query_result = await run_action(user, collection_name, query_input, db_session)

    # --- ASSERT: Verify multi-chunk ordering ---
    assert hasattr(query_result, "output_dict")
    single_block = query_result.output_dict["blocks"][0]

    # 1. Verify the answer contains info from start and end
    answer = single_block["text"].lower()
    assert "launch" in answer or "kennedy" in answer
    assert "landing" in answer or "touchdown" in answer

    # 2. Verify search data and sources
    search_data = single_block.get("search_data")
    assert search_data is not None, "search_data should be present"
    sources = search_data.get("sources", [])
    assert len(sources) > 1, f"Expected multiple chunks, but got {len(sources)}"

    # 3. CRITICAL: Verify chunks in graph have sequential 'order' values
    graph_viz = search_data.get("graph_visualization")
    assert graph_viz and "nodes" in graph_viz
    chunk_nodes = [n for n in graph_viz["nodes"] if n.get("type") == "Chunk"]
    assert len(chunk_nodes) > 1

    chunk_orders = []
    for node in chunk_nodes:
        props = node.get("details", {}).get("properties", {})
        assert "order" in props, f"Chunk node {node.get('id')} is missing 'order' property"
        chunk_orders.append(props["order"])

    chunk_orders.sort()

    # FIX: The document may have been split differently than expected by unstructured.io
    # (e.g., a title element might get order=0). What matters is that:
    # 1. Orders are unique
    # 2. Orders are sequential (no gaps)
    # 3. There are multiple chunks
    assert len(set(chunk_orders)) == len(chunk_orders), (
        f"Chunk orders should be unique, but got duplicates: {chunk_orders}"
    )

    # Check for sequential ordering (allowing any starting point)
    for i in range(1, len(chunk_orders)):
        assert chunk_orders[i] == chunk_orders[i - 1] + 1, (
            f"Chunk orders should be sequential. Gap detected: {chunk_orders[i - 1]} -> {chunk_orders[i]}"
        )

    # 4. Verify logical flow in the final answer
    launch_pos = answer.find("launch")
    landing_pos = answer.find("landing")
    if launch_pos != -1 and landing_pos != -1:
        assert launch_pos < landing_pos, (
            "Answer should maintain chronological order (launch before landing)"
        )


# -----------------------------------------------------------------
#  New Test Case: Media (Image) Ingestion End-to-End
# -----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skip("Crashing the system")
# @pytest.mark.skipif(
#     not settings.GEMINI_API_KEY,
#     reason="GEMINI_API_KEY is required for the media summarization strategy.",
# )
async def test_graphrag_action_media_ingestion_and_query(
    ctx, test_env, set_test_settings, db_session
):
    """
    Tests the full ingest-poll-query workflow for a media file (image).
    Since the 'synced_files' collection uses a text vectorizer, this test validates
    the "summarize" media strategy. The image content is described by a vision model,
    and that text description is ingested into both Weaviate and Neo4j.
    """
    user, collection_name, s3_client, temp_dir = test_env

    # --- ARRANGE: Define media asset and upload to S3 ---
    asset = {
        "url": "https://theoryofmaterials.com/wp-content/uploads/2025/05/AndrewBurgessPoster_DFT2024-scaled.jpg",
        "filename": f"dft_poster_{uuid.uuid4().hex[:8]}.jpg",
        "question": "What is the 'flat plane condition' and what problem do approximate exchange-correlation functionals face?",
        "expected_concepts": {
            "flat_plane": ["flat plane condition", "linearity"],
            "xc_problem": ["delocalisation error", "self-interaction error", "many-electron"],
        },
    }

    s3_url = await download_and_upload_media(ctx, asset, s3_client, temp_dir)
    await _wait_until_public(s3_url)

    # --- ACT 1: Ingest the media file ---
    ingest_input = DataBlock(urls=[s3_url]).model_dump()
    ingest_result = await run_action(user, collection_name, ingest_input, db_session)

    assert "Successfully queued" in ingest_result.output_dict["blocks"][0]["text"]
    job_doc = await db_session["IngestionJobs"].find_one(
        {"user_id": str(user.id), "original_filename": asset["filename"]},
        sort=[("created_at", -1)],
    )
    assert job_doc, "The media ingestion job was not created in the database."

    # --- ACT 2: Poll for job completion (requiring both backends) ---
    final_status = await poll_for_job_completion(
        job_doc["_id"],
        db=db_session,
        timeout_seconds=300,  # Media summarization can take longer
        required_backends=["weaviate", "graphrag"],
    )

    if final_status.get("status") != JobState.AVAILABLE.value:
        print("\n\n---- DETECTIVE WORK: DUMPING RECENT JOBS (media_ingestion_and_query) ----")
        cursor = db_session["IngestionJobs"].find({}).sort("created_at", -1).limit(10)
        async for doc in cursor:
            print(
                f"  - Job: {doc.get('_id')}, File: {doc.get('original_filename', 'N/A')}, Status: {doc.get('status', 'N/A')}, Error: {doc.get('error_message', 'None')}"
            )
        print("----------------------------------------------------------------------\n")

    assert final_status["status"] == JobState.AVAILABLE.value, (
        f"Media ingestion job failed: {final_status.get('error_message')}"
    )

    # --- ACT 3: Query about the content of the ingested media ---
    query_input = DataBlock(text=asset["question"]).model_dump()
    query_result = await run_action(user, collection_name, query_input, db_session)

    # --- ASSERT 3: Verify the RAG response based on the generated summary ---
    assert hasattr(query_result, "output_dict")
    single_block = query_result.output_dict.get("blocks", [])[0]
    final_answer = single_block["text"]
    assert "couldn't find" not in final_answer.lower(), "RAG query returned an empty/failed result."

    # 3a. Verify answer content
    logger.info(f"\nQuestion: {asset['question']}\nFinal Answer:\n{final_answer}")
    assert_concepts_covered(final_answer, asset["expected_concepts"])

    # 3b. Verify search metadata
    search_data = single_block.get("search_data")
    assert search_data is not None
    sources = search_data.get("sources", [])
    assert len(sources) > 0
    first_source = sources[0]
    assert first_source["source"] == asset["filename"]
    assert first_source["s3_url"] and first_source["s3_url"].startswith("https://")

    # 3c. Verify the chunk_type in the graph visualization, not a non-existent field
    graph_viz = search_data.get("graph_visualization")
    assert graph_viz and "nodes" in graph_viz, "Graph visualization data is missing"
    chunk_node = next((n for n in graph_viz["nodes"] if n.get("type") == "Chunk"), None)
    assert chunk_node is not None, "Expected a Chunk node in the graph visualization"
    chunk_props = (chunk_node.get("details") or {}).get("properties") or {}
    assert "chunk_type" in chunk_props and isinstance(chunk_props["chunk_type"], str), (
        "Chunk node in graph is missing a valid 'chunk_type' property"
    )

    # 3d. Verify data persistence in Weaviate
    weaviate_client = WeaviateVectorDBClient(
        url=settings.WEAVIATE_URL, api_key=settings.WEAVIATE_API_KEY, skip_init_checks=True
    )
    try:
        collection = weaviate_client.client.collections.get(name=collection_name)
        agg = await asyncio.to_thread(
            collection.with_tenant(str(user.id)).aggregate.over_all, total_count=True
        )
        assert agg.total_count > 0, "No data found in Weaviate for this tenant."
    finally:
        await weaviate_client.close()

    # 3e. Verify data persistence in Neo4j
    neo4j_client = await Neo4jManager.get_client()
    neo4j_result = await asyncio.to_thread(
        neo4j_client.query,
        f"MATCH (n:Chunk {{tenantId: '{user.id}'}}) RETURN count(n) as count",
        str(user.id),
    )
    assert neo4j_result and neo4j_result[0]["count"] > 0, (
        "No chunk nodes found in Neo4j for this tenant."
    )

    logger.success("✓ Media ingestion and query test completed successfully.")


@pytest.mark.asyncio
@pytest.mark.skip("Crashing the system")
async def test_graphrag_action_scanned_pdf_with_embedded_image(
    ctx, test_env, set_test_settings, db_session, monkeypatch
):
    """
    Tests the full ingest-poll-query workflow for a scanned PDF (which is an image).
    This test is CRITICAL for verifying that the Unstructured.io strategy (`auto` or `hi_res`)
    is correctly triggering image extraction, which is then summarized by Gemini (via OCR).

    EXPECTED OUTCOMES:
    - With strategy="auto" or "fast_with_gemini": This test SHOULD PASS.

    This test has been enhanced with four diagnostic assertions to make failures self-explanatory:
    1.  **Strategy Check**: Verifies the worker is configured with correct strategy before starting.
    2.  **Ingestion Check**: After polling, verifies that the ingestion job actually produced content (`chunk_uuids`).
    3.  **Retrieval Check**: After the RAG query, verifies that the retrieved sources are from the correct document.
    4.  **Answer Check**: A two-part check that first ensures the LLM didn't give up, then verifies the answer's content.
    """
    user, collection_name, s3_client, temp_dir = test_env

    # --- ARRANGE: Define the scanned PDF asset and upload it to S3 ---
    asset = {
        "local_path": Path("tests/resources/files/Single_scanned_page.pdf"),
        "filename": f"engagement_letter_{uuid.uuid4().hex[:8]}.pdf",
        "question": "How many single release track options are granted to the company?",
        "expected_concepts": {
            "track_options": ["three", "3"],
        },
    }

    # --- DIAGNOSTIC ASSERTION 1: Verify a supported parsing strategy is active ---
    # This fails the test immediately if the environment is misconfigured.
    settings = SettingsProvider.sget()
    strategy = settings.INGESTION_WORKER_PARSING_STRATEGY
    assert strategy in ("auto", "fast_with_gemini"), (
        "FATAL: Test requires INGESTION_WORKER_PARSING_STRATEGY to be either "
        f"'auto' or 'fast_with_gemini', but found '{strategy}'. Check .env configuration."
    )

    # Ensure the source file exists before proceeding
    if not asset["local_path"].exists():
        pytest.fail(f"Test resource file not found: {asset['local_path']}")

    # Copy the file to the temp directory for upload
    temp_file_path = temp_dir / asset["filename"]
    shutil.copy(asset["local_path"], temp_file_path)

    with open(temp_file_path, "rb") as f:
        s3_url = await s3_client.upload_fileobj(ctx, f, asset["filename"])
        logger.success(f"Uploaded scanned PDF '{asset['filename']}' to S3: {s3_url}")

    # === DIAGNOSTIC MONKEYPATCHING: Intercept WeaviateProcessor to analyze elements ===
    from src.autobots.ingestion import weaviate_processor

    original_processor_process = weaviate_processor.WeaviateProcessor.process

    async def patched_processor_process(self, elements, metadata, *args, **kwargs):
        print("\n\n--- [DIAGNOSTIC] Unstructured Element Analysis ---")
        if elements:
            type_counts = Counter(el.get("type") for el in elements)
            print(f"Element Types Found: {dict(type_counts)}")

            found_critical_element = False
            for el in elements:
                if "three further single" in el.get("text", "").lower():
                    found_critical_element = True
                    print(
                        f"✅ SUCCESS: Critical sentence found in element of type: '{el.get('type')}'"
                    )
                    whitelist = ["Table", "NarrativeText", "Title", "UncategorizedText"]
                    if el.get("type") not in whitelist:
                        print(
                            f"❌ FAILURE: The element type '{el.get('type')}' is NOT in the ingestion whitelist. (Hypothesis A Confirmed)"
                        )

            if not found_critical_element:
                print("❌ FAILURE: Critical sentence was not found in ANY Unstructured element.")
        else:
            print("❌ FAILURE: Unstructured returned zero elements.")
        print("------------------------------------------------\n")

        result = await original_processor_process(self, elements, metadata, *args, **kwargs)

        print("\n--- [DIAGNOSTIC] Final Chunks to be Ingested ---")
        print(f"Number of chunks produced: {len(result.get('documents', []))}")
        print("---------------------------------------------------\n")
        return result

    monkeypatch.setattr(weaviate_processor.WeaviateProcessor, "process", patched_processor_process)
    # === END DIAGNOSTIC MONKEYPATCHING ===

    await _wait_until_public(str(s3_url))

    # --- ACT 1: Ingest the scanned PDF ---
    ingest_input = DataBlock(urls=[str(s3_url)]).model_dump()
    ingest_result = await run_action(user, collection_name, ingest_input, db_session)

    assert "Successfully queued" in ingest_result.output_dict["blocks"][0]["text"]
    job_doc = await db_session["IngestionJobs"].find_one(
        {"user_id": str(user.id), "original_filename": asset["filename"]},
        sort=[("created_at", -1)],
    )
    assert job_doc, "The scanned PDF ingestion job was not created in the database."

    # --- STABILIZATION FIX ---
    # Add a small delay to ensure the background worker has time to pick up the job
    # from Redis and update its status from QUEUED to PROCESSING before we start polling.
    await asyncio.sleep(5)  # 5 seconds is a safe buffer

    # --- ACT 2: Poll for job completion ---
    final_status = await poll_for_job_completion(
        job_doc["_id"],
        db=db_session,
        timeout_seconds=300,  # OCR can be slow
        required_backends=["weaviate", "graphrag"],
    )

    if final_status.get("status") != JobState.AVAILABLE.value:
        logger.error(f"Dumping job details on failure: {final_status}")

    assert final_status["status"] == JobState.AVAILABLE.value, (
        f"Scanned PDF ingestion job failed. This is expected if Unstructured strategy is 'fast'. Error: {final_status.get('error_message')}"
    )

    # --- DIAGNOSTIC ASSERTION 2: Verify that ingestion produced content ---
    # A job can be 'AVAILABLE' but have zero chunks, which is a silent failure.
    assert "chunk_uuids" in final_status and final_status["chunk_uuids"], (
        "INGESTION FAILED: The job completed but produced zero processable chunks. Check the parsing strategy and OCR output."
    )

    # --- ACT 3: Query about the OCR'd content ---
    query_input = DataBlock(text=asset["question"]).model_dump()
    try:
        query_result = await run_action(user, collection_name, query_input, db_session)
        # --- ASSERT 3: Verify the RAG response based on the OCR'd and summarized content ---
        assert hasattr(query_result, "output_dict")
        single_block = query_result.output_dict.get("blocks", [])[0]
        final_answer = single_block["text"]
        search_data = single_block.get("search_data")
        sources = (search_data or {}).get("sources", [])

        # --- DIAGNOSTIC ASSERTION 3: Verify that the RAG pipeline retrieved the correct document ---
        assert sources, (
            "RETRIEVAL FAILED: The RAG query returned zero sources. The ingested content could not be found via vector search."
        )
        # Use `any` to make the check robust against the order of returned sources.
        assert any(s["source"] == asset["filename"] for s in sources), (
            f"RETRIEVAL FAILED: The expected document ('{asset['filename']}') was not found in the retrieved sources. "
            f"Found these instead: {[s['source'] for s in sources]}"
        )

        # 3a. Verify the answer contains the key information from the image
        logger.info(f"\nQuestion: {asset['question']}\nFinal Answer:\n{final_answer}")

        # --- DIAGNOSTIC ASSERTION 4: Robustly verify the final answer ---
        # First, check for common failure phrases from the LLM.
        failure_phrases = [
            "not enough",
            "couldn't find",
            "insufficient",
            "cannot determine",
            "not specify",
        ]
        answer_lower = final_answer.lower().strip()
        llm_gave_up = any(phrase in answer_lower for phrase in failure_phrases)

        assert not llm_gave_up, (
            f"LLM FAILED TO ANSWER: The response indicates the information was missing from the context provided. The full answer was: '{final_answer}'"
        )

        # If the LLM didn't give up, then check for the correct content.
        assert_concepts_covered(final_answer, asset["expected_concepts"])

        logger.success("✓ Scanned PDF ingestion and query test completed successfully.")

    except AssertionError as e:
        # === POST-MORTEM DIAGNOSTIC on test failure ===
        print("\n\n--- [DIAGNOSTIC] Post-Mortem Analysis on Test Failure ---")
        print(f"Test failed with assertion: {e}")
        print("Running ground-truth check against Weaviate to distinguish Hypothesis A from C...")

        from src.autobots.conn.weaviate.weaviate_store import WeaviateStore

        document_id = hashlib.sha256(str(s3_url).encode()).hexdigest()
        store = None
        try:
            store = await WeaviateStore.create_store(collection_name, user_id=str(user.id))
            stored_docs = await store.fetch_objects_by_filter(
                {"document_id": document_id}, limit=100
            )
            print(
                f"Found {len(stored_docs)} chunks in Weaviate for document_id '{document_id[:12]}...'."
            )

            found_in_weaviate = False
            for i, doc in enumerate(stored_docs):
                if "three further single" in doc.page_content.lower():
                    found_in_weaviate = True
                    print(
                        f"✅ SUCCESS: Critical sentence FOUND in stored chunk {i}: '{doc.page_content[:100]}...'"
                    )

            if not found_in_weaviate:
                print(
                    "❌ CONCLUSION: Critical sentence is NOT in Weaviate. The issue is ingestion (Hypothesis A or B)."
                )
            else:
                print(
                    "✅ CONCLUSION: Critical sentence IS in Weaviate. The issue is RAG retrieval/ranking (Hypothesis C)."
                )
        finally:
            if store:
                await store.close()
        print("----------------------------------------------------------\n")
        raise  # Re-raise the original assertion error
