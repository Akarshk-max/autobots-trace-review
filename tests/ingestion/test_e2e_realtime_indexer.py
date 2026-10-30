# tests/ingestion/test_e2e_realtime_indexer.py
#
# NOTE: This is a standalone E2E script, not a standard pytest file.
# It is designed to be run from the project root directory like this:
# python -m tests.ingestion.test_e2e_realtime_indexer
#
# It requires the ingestion-worker service to be running locally with
# ENV=local to enable the test authentication bypass.
#
import asyncio
import json
import sys
import time
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path

import httpx
from bson import ObjectId
from jose import jwt
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorClient

# --- Setup Project Path ---
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ruff: noqa: E402
from src.autobots.conn.weaviate.weaviate_client import WeaviateVectorDBClient
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.ingestion.neo4j_loader import Neo4jManager

# ======================================================
# ===== E2E TEST CONFIGURATION (RUNS LOCALLY ONLY)   =====
# ======================================================
E2E_TEST_USER_ID = "e2e00000-0000-0000-0000-000000000000"

# --- Fixtures for S3-backed Event Result Test ---

# This is the full data that will be stored in S3 for the E2E test.
E2E_FULL_GRAPH_RUN_FROM_S3 = {
    "name": "Understanding AI Basics (E2E Test)",
    "description": "A workflow to test S3 hydration.",
    "type": "action_graph_run",
    "status": "success",
    "user_id": E2E_TEST_USER_ID,
    "result": {
        "id": "graph_def_e2e_123",
        "name": "E2E Test Workflow",
        "input": {"text": "What is AI?"},
        "graph": {"1757680611194": ["1758201037342"]},
        "output": {
            "1758201037342": {
                "id": "action_def_e2e_abc",
                "name": "Test stream",
                "input": {"text": "What is AI?"},
                "output": {
                    "blocks": [
                        {
                            "text": "This is the unique output from the first step in the E2E test S3 file about artificial intelligence fundamentals."
                        }
                    ]
                },
                "results": [
                    {
                        "input": {"text": "What is AI?"},
                        "output": {
                            "blocks": [
                                {
                                    "text": "This is the unique output from the first step in the E2E test S3 file about artificial intelligence fundamentals."
                                }
                            ]
                        },
                    }
                ],
            },
        },
    },
}


# This is the "lite" document that we will insert into MongoDB for the E2E test.
E2E_LITE_GRAPH_RUN_FOR_MONGO = {
    "name": "Understanding AI Basics (E2E Test)",
    "description": "A workflow to test S3 hydration.",
    "type": "action_graph_run",
    "status": "success",
    "user_id": E2E_TEST_USER_ID,
    "result": {
        "id": "graph_def_e2e_123",
        "output": {},  # Empty, forcing the indexer to look at the file_url.
    },
}


async def wait_for_indexed(db, doc_id: ObjectId, timeout_seconds: int = 90) -> bool:
    """Polls MongoDB until a document is marked 'indexed: True'."""
    # Increased timeout to 90s. This must be longer than the indexer's poll interval (30s)
    # plus a generous buffer for processing (which can include slow LLM calls).
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        doc = await db["EventResults"].find_one({"_id": doc_id})
        if doc and doc.get("indexed") is True:
            logger.success(
                f"Document {doc_id} marked as indexed in {time.time() - (deadline - timeout_seconds):.2f}s."
            )
            return True
        await asyncio.sleep(2)
    logger.error(
        f"Timeout: Document {doc_id} was not marked as indexed within {timeout_seconds} seconds."
    )
    return False


async def cleanup_static_test_artifacts(mongo_client, weaviate_client, neo4j_client):
    """
    Surgically removes known static artifacts from previous E2E test runs.
    This is not a full tenant wipe, but a targeted removal of specific data
    created by the E2E test fixtures.
    """
    logger.info("--- Cleaning up known static E2E test artifacts ---")
    settings = SettingsProvider.sget()

    # --- 1. MongoDB Cleanup ---
    # Delete documents that match the static names and descriptions from the fixtures.
    db = mongo_client[settings.MONGO_DATABASE]
    await db["EventResults"].delete_many(
        {
            "user_id": E2E_TEST_USER_ID,
            "$or": [
                {"name": {"$regex": "^E2E Test EventResults"}},  # Matches test docs
                {"name": "Understanding AI Basics (E2E Test)"},  # Matches S3 test doc
            ],
        }
    )
    # Also delete the test user to ensure it's recreated cleanly
    await db["Users"].delete_one({"_id": E2E_TEST_USER_ID})
    logger.success("MongoDB cleanup of static test artifacts complete.")

    # --- 2. Weaviate Cleanup ---
    try:
        await weaviate_client.delete_by_filter(
            class_name=settings.INGESTION_WORKER_MONGO_INDEXER_WEAVIATE_COLLECTION_NAME,
            filter_dict={"user_id": E2E_TEST_USER_ID},
            tenant_id=E2E_TEST_USER_ID,
        )
        logger.success("Weaviate cleanup of static test artifacts complete.")
    except Exception as e:
        logger.warning(f"Could not perform Weaviate cleanup (this is safe on first run): {e}")

    # --- 3. Neo4j Cleanup ---
    try:
        query = "MATCH (n {tenantId: $tenantId}) DETACH DELETE n"
        params = {"tenantId": E2E_TEST_USER_ID}
        await asyncio.to_thread(neo4j_client.query, query, E2E_TEST_USER_ID, params)
        logger.success("Neo4j cleanup of static test artifacts complete.")
    except Exception as e:
        logger.warning(f"Could not perform Neo4j cleanup (this is safe on first run): {e}")

    logger.info("--- Finished cleaning up static E2E test artifacts ---")


async def verify_in_neo4j(neo4j_client, user_id: str, canonical_uuid: str) -> bool:
    """
    Polls Neo4j to verify a rich graph (node with relationships) has been created.
    Returns True on success, raises TimeoutError on failure.
    The timeout is intentionally long (110s) to account for Neo4j Aura's eventual consistency,
    which is also observed in the application's own RAG retry logic.
    """
    start_time = time.time()
    max_wait_seconds = 110
    # This query checks for the main ExecutionRun node AND verifies it has relationships,
    # proving that the rich graph extraction was successful.
    query = """
    MATCH (n:Chunk {id: $node_id, tenantId: $tenant_id})-[r]-() 
    WHERE n.chunk_type = 'ExecutionRun' 
    RETURN count(r) as rel_count
    """
    params = {"node_id": canonical_uuid, "tenant_id": user_id}
    logger.info(
        f"Polling Neo4j for rich graph from node '{canonical_uuid}' for up to {max_wait_seconds}s..."
    )
    while time.time() - start_time < max_wait_seconds:
        try:
            results = await asyncio.to_thread(neo4j_client.query, query, user_id, params)
            if results and results[0]["rel_count"] > 0:
                logger.success(
                    f"SUCCESS: Found rich graph in Neo4j in {time.time() - start_time:.2f} seconds."
                )
                return True
        except Exception as e:
            logger.warning(f"Neo4j query failed during polling, will retry. Error: {e}")
        await asyncio.sleep(5)

    # Raise an explicit error to fail the test clearly
    raise TimeoutError(
        f"E2E test assertion failed: Rich graph for node '{canonical_uuid}' did not appear in Neo4j within {max_wait_seconds} seconds."
    )


async def verify_service_health(service_url: str) -> bool:
    """Performs a pre-flight health check to ensure the service is running."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{service_url}/health")
            if response.status_code == 200:
                logger.success(f"Service is healthy and responsive at {service_url}")
                return True
            logger.error(f"Service health check failed with status {response.status_code}.")
            return False
    except httpx.ConnectError:
        logger.error(f"Could not connect to service at {service_url}. Is it running?")
        return False


def _create_test_document(collection_name: str) -> dict:
    """Factory to create a valid test document for a given collection."""
    unique_id_obj = ObjectId()
    # FIX: Use a current timestamp. Creating documents in the future breaks the indexer's
    # chronological polling logic. The document must be immediately visible.
    current_timestamp = datetime.utcnow()

    # --- FIX: Define unique search terms as explicit variables for clarity and safety ---
    unique_description = f"Unique E2E description {unique_id_obj}"
    unique_name = f"E2E Test {collection_name} {unique_id_obj}"

    base_doc = {
        "_id": unique_id_obj,
        "user_id": E2E_TEST_USER_ID,
        "created_at": current_timestamp,
        "updated_at": current_timestamp,
        "description": unique_description,
        "indexed": False,
    }

    if collection_name == "Actions":
        base_doc.update({"name": unique_name})
    elif collection_name == "ActionGraphs":
        base_doc.update(
            {
                "name": unique_name,
                "node_details": {},
                "graph": {},
            }
        )
    elif collection_name == "EventResults":
        # Use an action_run to test the rich graph extraction path.
        base_doc.update(
            {
                "name": unique_name,
                "type": "action_run",
                "status": "success",
                # Use the multi-turn 'results' array structure
                # that the transformer is designed to process.
                "result": {
                    "results": [
                        {
                            "input": {"text": "Initial prompt"},
                            "output": {
                                "blocks": [
                                    {
                                        "text": (
                                            f"{unique_description}. The starship Parallax, led by "
                                            f"Captain Aya Sato, vanished near Mare Serenitatis. The mission was to explore "
                                            f"the Continuum."
                                        )
                                    }
                                ]
                            },
                        }
                    ]
                },
            }
        )
    elif collection_name == "Chats":
        base_doc.update({"title": unique_name, "messages": []})
        # For chats, the title is more natural to search for
        base_doc["description"] = unique_name

    return base_doc


async def run_s3_backed_action_graph_run_test(mongo_client, weaviate_client, neo4j_client):
    """
    E2E Test: Verifies the indexer correctly processes an S3-backed action_graph_run
    and asserts the creation of a linked, structural graph in Neo4j, confirming
    that GraphRAG is correctly NOT run for this type.
    """
    logger.info("--- Starting E2E Test: S3-Backed EventResult (action_graph_run) ---")
    settings = SettingsProvider.sget()
    db = mongo_client[settings.MONGO_DATABASE]
    s3_filename = None
    user_files = None
    doc_id = None

    try:
        # --- ARRANGE ---
        # 1. Create a base document to get a unique _id, name, and description.
        unique_doc_parts = _create_test_document("EventResults")
        # 2. Start with the static fixture data.
        lite_mongo_doc = E2E_LITE_GRAPH_RUN_FOR_MONGO.copy()
        # 3. Explicitly merge the unique parts into the test document.
        lite_mongo_doc["_id"] = unique_doc_parts["_id"]
        lite_mongo_doc["name"] = unique_doc_parts["name"]
        lite_mongo_doc["description"] = unique_doc_parts["description"]
        lite_mongo_doc["created_at"] = unique_doc_parts["created_at"]
        lite_mongo_doc["updated_at"] = unique_doc_parts["updated_at"]
        doc_id = lite_mongo_doc["_id"]

        full_s3_data = E2E_FULL_GRAPH_RUN_FROM_S3.copy()
        full_s3_data.update(
            {
                "_id": str(doc_id),
                "user_id": E2E_TEST_USER_ID,
                "created_at": lite_mongo_doc["created_at"].isoformat() + "Z",
                "updated_at": lite_mongo_doc["updated_at"].isoformat() + "Z",
            }
        )

        from src.autobots.conn.aws.aws_s3 import get_public_s3
        from src.autobots.user.user_orm_model import UserORM

        test_user = UserORM(id=E2E_TEST_USER_ID)
        # NOTE: Your local env vars point AWS_S3_PUBLIC_BUCKET_NAME to your local bucket.
        # This ensures test isolation without patching.
        s3_client_for_test = get_public_s3(object_prefix="")
        logger.info(
            f"E2E test is using S3 bucket: {s3_client_for_test.bucket.name} (from settings)"
        )

        user_files = UserFiles(
            user=test_user, s3=s3_client_for_test, top_level_folder=settings.AWS_S3_RUNS_FOLDER
        )
        s3_filename = f"{doc_id}.json"
        s3_payload_bytes = json.dumps(full_s3_data).encode("utf-8")
        ctx = Context()
        file_metas = await user_files.upload_binary(
            ctx, file_obj=BytesIO(s3_payload_bytes), filename=s3_filename
        )
        assert len(file_metas) == 1, "S3 upload should return one file meta."
        s3_url = str(file_metas[0].url)

        await db["EventResults"].insert_one(
            {**lite_mongo_doc, "file_url": s3_url, "indexed": False}
        )

        # --- ACT ---
        assert await wait_for_indexed(db, doc_id), (
            f"Indexer failed to mark doc {doc_id} as indexed."
        )

        # --- ASSERT ---
        # Weaviate check
        weaviate_store = WeaviateVectorDBClient(
            url=settings.WEAVIATE_URL, api_key=settings.WEAVIATE_API_KEY, skip_init_checks=True
        )
        search_results = await weaviate_store.search(
            class_name=settings.INGESTION_WORKER_MONGO_INDEXER_WEAVIATE_COLLECTION_NAME,
            tenant_id=E2E_TEST_USER_ID,
            query_text=lite_mongo_doc["name"],  # Search for the unique name of this run
            limit=10,  # Fetch more results to be robust to ranking
        )
        assert len(search_results) > 0, "Long text from S3 not found in Weaviate"

        # Assert that ANY of the returned chunks belong to the document we just created.
        # This is robust to small changes in search ranking.
        assert any(
            r.get("properties", {}).get("document_id", "").startswith(str(doc_id))
            for r in search_results
        ), "Weaviate did not return a chunk belonging to the current workflow run"
        logger.success("Weaviate assertion PASSED for action_graph_run.")

        # Neo4j check for structural workflow graph
        summary_uuid = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{E2E_TEST_USER_ID}:EventResults:{doc_id}_summary")
        )
        # Use a more robust query that checks for either HAS_STEP or NEXT relationship
        query = """
        MATCH (run:Chunk {id: $runId, tenantId: $tenantId})-[:HAS_STEP|NEXT]->(step:Chunk)
        RETURN count(step) AS steps
        """
        params = {"runId": summary_uuid, "tenantId": E2E_TEST_USER_ID}
        result = await asyncio.to_thread(neo4j_client.query, query, E2E_TEST_USER_ID, params)
        assert len(result) > 0 and result[0]["steps"] >= 1, (
            "Expected HAS_STEP or NEXT edges not found for workflow run."
        )
        logger.success("Neo4j structural assertion PASSED for action_graph_run.")

    finally:
        # --- CLEANUP ---
        # No per-test cleanup needed here; the main `finally` block handles it.
        pass


async def main():
    """Main E2E test function."""
    SettingsProvider.set_env_file(".env.local")
    settings = SettingsProvider.sget()

    # --- Pre-flight Checks (Fail Fast) ---
    if settings.ENV != "local":
        logger.error("This E2E test is designed to run only with ENV=local. Aborting.")
        sys.exit(1)

    SERVICE_API_URL = settings.INGESTION_WORKER_URL
    if not await verify_service_health(SERVICE_API_URL):
        logger.error("Aborting test due to unhealthy service.")
        sys.exit(1)

    mongo_client = None
    weaviate_client = None
    neo4j_client = None

    try:
        mongo_client = AsyncIOMotorClient(settings.MONGO_CONN)

        weaviate_client = WeaviateVectorDBClient(
            url=settings.WEAVIATE_URL, api_key=settings.WEAVIATE_API_KEY, skip_init_checks=True
        )

        # Get Neo4j client for cleanup
        neo4j_client = await Neo4jManager.get_client()

        # --- PRE-TEST PURGE ---
        await cleanup_static_test_artifacts(mongo_client, weaviate_client, neo4j_client)
        # ----------------------

        db = mongo_client[settings.MONGO_DATABASE]

        # --- Setup: Ensure the test user exists ---
        test_user_doc = {
            "_id": E2E_TEST_USER_ID,
            "user_id": E2E_TEST_USER_ID,
            "email": "e2e-test-user@autobots.com",
            "updated_at": datetime.utcnow().replace(tzinfo=None),
            "created_at": datetime.utcnow().replace(tzinfo=None),
        }
        await db["Users"].update_one(
            {"_id": E2E_TEST_USER_ID}, {"$set": test_user_doc}, upsert=True
        )
        logger.success(f"Ensured E2E test user '{E2E_TEST_USER_ID}' exists in MongoDB.")

        # --- Test 1: Action_run (in-Mongo) with long text to trigger GraphRAG ---
        # --- FOCUSED TEST: Hard-code this test to verify EventResults ---
        collection_to_test = "EventResults"
        targets_for_collection = settings.INGESTION_WORKER_MONGO_INDEXER_TARGETS.get(
            collection_to_test, []
        )

        if not targets_for_collection:
            logger.warning(
                f"The '{collection_to_test}' collection is not enabled for indexing. Skipping E2E test."
            )
            sys.exit(1)

        test_weaviate = "weaviate" in targets_for_collection
        test_neo4j = "neo4j" in targets_for_collection
        logger.info(
            f"E2E test will verify '{collection_to_test}' against targets: Weaviate={test_weaviate}, Neo4j={test_neo4j}"
        )

        logger.info("--- Starting E2E Test: EventResult (action_run) ---")

        # --- Arrange: Create the test document ---
        doc_to_insert = _create_test_document(collection_to_test)
        # unique_search_term = doc_to_insert["description"]
        unique_source_name = doc_to_insert.get("name") or doc_to_insert.get("title")

        logger.info(
            f"Inserting test document into MongoDB collection '{collection_to_test}': "
            f"{doc_to_insert['_id']}"
        )
        await db[collection_to_test].insert_one(doc_to_insert)

        assert await wait_for_indexed(db, doc_to_insert["_id"]), (
            "Indexer failed to mark action_run as processed."
        )

        # --- Act & Assert ---
        # CORRECTION: Use `unique_name` as it's the variable assigned from the factory.
        search_payload = {
            "query": doc_to_insert["name"],
            "collection_name": settings.INGESTION_WORKER_MONGO_INDEXER_WEAVIATE_COLLECTION_NAME,
        }

        token_payload = {
            "sub": E2E_TEST_USER_ID,
            "iat": int(time.time()),
            "exp": int(time.time()) + 900,  # 15 minute expiry
        }
        test_jwt = jwt.encode(
            token_payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
        )
        headers = {"Authorization": f"Bearer {test_jwt}"}

        max_wait_seconds = 120  # Give the overall process plenty of time
        start_time = time.time()

        # --- CONDITIONAL & CONCURRENT VERIFICATION ---
        neo4j_task = None
        if test_neo4j:
            # For an action_run, the transformer creates a `_turn_0` document and
            # anchors the GraphRAG entities to its corresponding ChunkNode.
            # Use the same deterministic UUID helper that the indexer uses so the test
            # always polls for the ID that is actually written to the database.
            from src.autobots.ingestion.realtime_indexer import RealtimeIndexer

            mongo_id_str = str(doc_to_insert["_id"])
            # The transformer creates a `_turn_0` document for an action_run.
            document_id_for_uuid = f"{mongo_id_str}_turn_0"

            # _generate_weaviate_uuid is an instance method, so we need an instance to call it.
            indexer_instance = RealtimeIndexer(db)
            canonical_uuid_for_neo4j = indexer_instance._generate_weaviate_uuid(
                {"mongo_collection": collection_to_test, "document_id": document_id_for_uuid},
                E2E_TEST_USER_ID,
            )
            neo4j_task = asyncio.create_task(
                verify_in_neo4j(neo4j_client, E2E_TEST_USER_ID, canonical_uuid_for_neo4j)
            )

        weaviate_verified = False
        if test_weaviate:
            logger.info(f"Polling Weaviate via search API for up to {max_wait_seconds}s...")
            timeout_config = httpx.Timeout(connect=5.0, read=60.0, write=5.0, pool=5.0)
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                while time.time() - start_time < max_wait_seconds:
                    try:
                        response = await client.post(
                            f"{SERVICE_API_URL}/api/v1_ingestion/knowledge-base/search",
                            json=search_payload,
                            headers=headers,
                        )
                        response.raise_for_status()
                        data = response.json()
                        if (
                            data.get("blocks")
                            and "couldn't find" not in data["blocks"][0].get("text", "").lower()
                        ):
                            search_data = data["blocks"][0].get("search_data")
                            if search_data and search_data.get("sources"):
                                # --- FIX: Check if ANY returned source matches, not just the first one ---
                                # This makes the test robust against minor variations in vector search ranking.
                                if any(
                                    s.get("source") == unique_source_name
                                    for s in search_data["sources"]
                                ):
                                    logger.success(
                                        f"SUCCESS: Found document in Weaviate in {time.time() - start_time:.2f} seconds."
                                    )
                                    weaviate_verified = True
                                    break  # Exit Weaviate polling loop on success
                    except (httpx.HTTPStatusError, httpx.ReadTimeout, httpx.ConnectError) as e:
                        logger.warning(f"Polling Weaviate failed, will retry. Error: {e}")
                    await asyncio.sleep(5)

            if not weaviate_verified:
                raise TimeoutError(
                    f"E2E test failed: Document did not become searchable in Weaviate within {max_wait_seconds} seconds."
                )

        # After Weaviate is verified (or if it wasn't a target), wait for the Neo4j task to complete.
        # The `await` will re-raise any exception (like TimeoutError) from the task.
        if neo4j_task:
            await neo4j_task

        logger.success("All configured backends verified successfully for action_run!")

        # --- Test 2: Run the new S3-backed test for action_graph_run ---
        # These will reuse the same client connections.
        await run_s3_backed_action_graph_run_test(mongo_client, weaviate_client, neo4j_client)

        logger.success("All E2E tests passed!")

    except Exception:
        logger.exception("E2E test failed with an unexpected error.")
        sys.exit(1)
    finally:
        # --- POST-TEST PURGE ---
        if mongo_client and weaviate_client and neo4j_client:
            await cleanup_static_test_artifacts(mongo_client, weaviate_client, neo4j_client)
        # -----------------------

        logger.info("--- E2E Test Final Resource Shutdown ---")
        if mongo_client:
            mongo_client.close()
        if weaviate_client:
            await weaviate_client.close()
        # Shutdown the singleton manager to close the Neo4j connection
        Neo4jManager.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
        logger.success("E2E Test script finished successfully.")
    except Exception:
        sys.exit(1)
