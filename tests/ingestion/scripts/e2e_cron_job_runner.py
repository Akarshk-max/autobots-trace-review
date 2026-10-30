# tests/ingestion/scripts/e2e_cron_job_runner.py

import asyncio
import sys
import uuid
from pathlib import Path

import httpx
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

# --- Setup Project Path ---
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ruff: noqa: E402
from src.autobots.conn.aws.aws_s3 import get_aws_s3
from src.autobots.conn.weaviate.weaviate_client import WeaviateVectorDBClient
from src.autobots.core.database.mongo_base import close_mongo_client, get_mongo_client
from src.autobots.core.logging.setup_logger import setup_logger
from src.autobots.core.settings import SettingsProvider
from src.autobots.ingestion.neo4j_loader import Neo4jManager
from src.autobots.user.user_orm_model import UserORM

####################################################################################
# E2E TEST FOR NIGHTLY SYNC CRON JOB
#
# IMPORTANT: MANUAL SETUP REQUIRED
# This test verifies the end-to-end functionality of the scheduled S3 sync job.
#
# HOW TO RUN:
# 1. Configure `src/autobots/core/settings.py` or `.env.local` file (or environment variables):
#    - Set INGESTION_WORKER_S3_SYNC_MODE="WHITELIST"
#    - Ensure your test user ID is in the INGESTION_WORKER_S3_SYNC_WHITELIST_USER_IDS list.
#
# 2. Open the file: `src/autobots/core/lifespan_ingestion.py`
#    Find the `Schedule._scheduler.add_job` call for the `run_nightly_s3_sync` job.
#    Temporarily change the cron trigger to run every minute for fast testing.
#
#    CHANGE FROM (Production):
#    trigger="cron",
#    hour=2,  # or hour="*" for hourly
#    minute=0,
#
#    CHANGE TO (Testing):
#    trigger="cron",
#    minute="*/1",
#
# 3. Start your local Docker environment:
#    $ bash ./scripts/dev/start-cloud-dev.sh
#
# 4. Run this script from the project root:
#    $ python -m tests.ingestion.scripts.e2e_cron_job_runner
#
#    - Run it from inside a container via `docker-compose exec ...`.
#
# 5. AFTER TESTING: Remember to revert the cron trigger in the worker back to production!
####################################################################################


async def run_test(db: AsyncIOMotorDatabase):
    """
    An E2E test that verifies the entire nightly sync workflow.
    Assumes the cron job has been manually set to a 1-minute interval and
    that the Docker containers are already running.
    """
    settings = SettingsProvider.sget()
    # Use the specific user ID that the cron job is configured to scan in WHITELIST mode.
    # This ID should be present in the INGESTION_WORKER_S3_SYNC_WHITELIST_USER_IDS setting.
    user = UserORM(id="56dd105c-606e-4f6d-8e88-8001d8f4f552")
    collection_name = "synced_files"  # This is the hardcoded collection for the sync job
    test_file_content = f"The secret codename for project {uuid.uuid4().hex} is KESTREL."

    s3_client = get_aws_s3(
        settings.AWS_S3_BUCKET_REGION,
        settings.AWS_ACCESS_KEY_ID,
        settings.AWS_SECRET_ACCESS_KEY,
        settings.AWS_S3_PUBLIC_BUCKET_NAME,
    )

    # Use a unique filename for each test run to further prevent state collision
    s3_key = f"files/{user.id}/e2e_cron_test_{uuid.uuid4().hex[:8]}.txt"

    try:
        # --- ARRANGE ---
        logger.info(f"Uploading test file to s3://{s3_client.bucket.name}/{s3_key}")

        # --- Pre-emptive Cleanup ---
        # Ensure any stale log entry for this exact file from a previously
        # failed test run is deleted before we start. This makes the test idempotent.
        processed_files_collection = db["processed_files_log"]
        delete_result = await processed_files_collection.delete_one({"_id": s3_key})
        if delete_result.deleted_count > 0:
            logger.warning(f"Removed a stale log entry for '{s3_key}' before starting the test.")

        await asyncio.to_thread(
            s3_client.bucket.put_object, Key=s3_key, Body=test_file_content.encode("utf-8")
        )

        # Add a short delay to ensure the uploaded file is visible in S3 listings
        # before the next cron job cycle begins (S3 Eventual Consistency).
        logger.info("Waiting 10s for S3 to achieve consistency...")
        await asyncio.sleep(10)

        # --- ACT ---
        logger.info(
            "Waiting for the 1-minute cron job to trigger and the worker to process... (will wait up to 90s)"
        )
        await asyncio.sleep(90)  # Wait long enough for the cron to fire and job to be processed.

        # --- ASSERT ---
        logger.info("Asserting by querying the RAG service...")
        search_payload = {
            "query": "What is the secret codename?",
            "collection_name": collection_name,
        }

        from datetime import datetime, timedelta

        from jose import jwt

        expire = datetime.utcnow() + timedelta(minutes=5)
        to_encode = {"sub": str(user.id), "exp": int(expire.timestamp())}
        mock_token = jwt.encode(
            to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
        )
        headers = {"Authorization": f"Bearer {mock_token}"}

        # Use the INGESTION_WORKER_URL from settings instead of a hardcoded localhost.
        # This allows the script to work correctly from inside the container.
        service_url = (
            f"{settings.INGESTION_WORKER_URL}/api{settings.API_V1_INGESTION}/knowledge-base/search"
        )

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(service_url, json=search_payload, headers=headers)
            response.raise_for_status()
            result = response.json()

        final_answer = result["blocks"][0]["text"].lower()
        logger.info(f"RAG Service Answer: '{final_answer}'")
        assert "kestrel" in final_answer, "The ingested content was not found in the RAG response."

        logger.success("--- E2E Cron Job Test PASSED! ---")

    except Exception as e:
        logger.error("--- E2E Cron Job Test FAILED ---")
        logger.exception(e)
        # Exit with a non-zero code to indicate failure
        sys.exit(1)

    finally:
        # --- CLEANUP ---
        logger.info("--- Starting E2E Test Cleanup ---")

        await asyncio.to_thread(
            s3_client.bucket.delete_objects, Delete={"Objects": [{"Key": s3_key}]}
        )
        logger.info(f"Deleted S3 object: {s3_key}")

        weaviate_client = WeaviateVectorDBClient(
            url=settings.WEAVIATE_URL, api_key=settings.WEAVIATE_API_KEY, skip_init_checks=True
        )
        collection = weaviate_client.client.collections.get(name=collection_name)
        if await asyncio.to_thread(collection.tenants.exists, str(user.id)):
            await asyncio.to_thread(collection.tenants.remove, [str(user.id)])
            logger.info(f"Deleted Weaviate tenant: {user.id}")
        await weaviate_client.close()

        neo4j_client = await Neo4jManager.get_client()
        await asyncio.to_thread(neo4j_client.delete_all_tenant_data, tenant_id=str(user.id))
        logger.info(f"Deleted Neo4j data for tenant: {user.id}")
        Neo4jManager.shutdown()


async def main():
    """Main entry point to set up the environment and run the test."""
    setup_logger()
    SettingsProvider.set_env_file(".env.local")
    SettingsProvider.set()

    # Create the client at the highest level of the script
    mongo_client = get_mongo_client()
    try:
        # Get the database object from the client
        db = mongo_client[SettingsProvider.sget().MONGO_DATABASE]
        # Pass the database object to the test function
        await run_test(db=db)
    finally:
        # Ensure the client is closed when the script is done
        close_mongo_client()
        logger.info("MongoDB client for test script closed cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
