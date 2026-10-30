# tests/ingestion/scripts/e2e_mt_runner.py

import asyncio
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from jose import jwt
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

# --- Setup Project Path ---
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ruff: noqa: E402
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_multimodal.action_multimodal_graph_rag_query import (
    ActionGraphRAGQuery,
    GraphRAGQueryConfig,
)
from src.autobots.conn.aws.aws_s3 import get_aws_s3
from src.autobots.conn.weaviate.weaviate_client import WeaviateVectorDBClient
from src.autobots.core.logging.setup_logger import setup_logger
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.ingestion.neo4j_loader import Neo4jManager
from src.autobots.ingestion.polling import poll_for_job_completion, readiness_probe
from src.autobots.ingestion.service import IngestionService
from src.autobots.user.user_orm_model import UserORM

# ==========================================================
# === TEST CONFIGURATION & HELPERS =========================
# ==========================================================

# Define test data for two separate tenants
TENANT_A_DATA = {
    "user": UserORM(id=str(uuid.uuid4())),
    "files": {
        "phoenix_protocol.txt": "Project Phoenix is a go. The primary objective is alpha stabilization.",
        "phoenix_members.txt": "The Phoenix team includes Dr. Evelyn Reed and Agent Jack Hudson.",
    },
    "query": "Who is on the Project Phoenix team?",
    "expected_keywords": ["evelyn reed", "jack hudson"],
    "forbidden_keywords": ["chimera", "dr. aris"],
}

TENANT_B_DATA = {
    "user": UserORM(id=str(uuid.uuid4())),
    "files": {
        "chimera_directive.txt": "Project Chimera is now active. The goal is beta containment.",
        "chimera_personnel.txt": "Dr. Aris Thorne is the lead for the Chimera initiative.",
    },
    "query": "Who is the lead for Project Chimera?",
    "expected_keywords": ["aris thorne", "chimera"],
    "forbidden_keywords": ["phoenix", "evelyn reed"],
}


async def count_weaviate_objects(collection_name: str, tenant_id: str) -> int:
    """Helper to count objects in a specific Weaviate tenant."""
    settings = SettingsProvider.sget()
    client = WeaviateVectorDBClient(
        url=settings.WEAVIATE_URL, api_key=settings.WEAVIATE_API_KEY, skip_init_checks=True
    )
    try:
        collection = client.client.collections.get(name=collection_name)
        response = collection.with_tenant(tenant_id).aggregate.over_all(total_count=True)
        return response.total_count
    finally:
        await client.close()


async def count_neo4j_nodes(tenant_id: str, label: str = "Chunk") -> int:
    """Helper to count nodes for a specific tenant in Neo4j."""
    client = await Neo4jManager.get_client()
    query = f"MATCH (n:{label} {{tenantId: $tenantId}}) RETURN count(n) as count"
    result = await asyncio.to_thread(client.query, query, tenant_id, params={"tenantId": tenant_id})
    return result[0]["count"] if result else 0


# ==========================================================
# === MAIN TEST EXECUTION LOGIC ============================
# ==========================================================


async def run_programmatic_mt_e2e_test(db: AsyncIOMotorDatabase):
    """
    A comprehensive E2E test to verify PROGRAMMATIC creation of multi-tenant collections.
    """
    logger.info("--- Starting Programmatic Multi-Tenancy E2E Test ---")
    ctx = Context()

    # Use a single, unique, and valid collection name for this test run.
    # It must start with an uppercase letter, and can only contain alphanumeric characters and underscores
    collection_name = f"E2eProgrammaticMt_{uuid.uuid4().hex[:8]}"
    temp_dir = Path(tempfile.mkdtemp())

    # Store S3 prefixes for cleanup
    s3_prefixes_to_clean = []

    try:
        # --- PRE-TEST CLEANUP: Ensure a clean slate for this test run ---
        logger.info("Cleaning IngestionJobs collection before test run...")
        await db["IngestionJobs"].delete_many({})

        # ===================================================================
        # PHASE 1: INGESTION FOR BOTH TENANTS
        # ===================================================================
        logger.info("--- PHASE 1A: QUEUE ALL JOBS ---")
        all_job_ids = []

        for tenant_data in [TENANT_A_DATA, TENANT_B_DATA]:
            user = tenant_data["user"]
            user_id = str(user.id)
            s3_prefix = f"e2e-testing/files/{user_id}/"
            s3_prefixes_to_clean.append(s3_prefix)

            logger.info(f"Queueing jobs for Tenant: {user_id}")

            ingestion_service = IngestionService(user_id=user_id)
            s3_client = ingestion_service.s3_client  # Use the service's S3 client

            for filename, content in tenant_data["files"].items():
                local_file_path = temp_dir / filename
                local_file_path.write_text(content)

                # Upload to user's scoped S3 folder
                with open(local_file_path, "rb") as f:
                    s3_key = f"{s3_prefix}{filename}"
                    await asyncio.to_thread(s3_client.bucket.upload_fileobj, f, s3_key)

                # Queue the job for ingestion
                job_id = await ingestion_service.queue_ingestion_from_s3(
                    s3_key=s3_key,
                    original_filename=filename,
                    collection_name=collection_name,
                    db=db,
                    targets=["weaviate", "graphrag"],
                )
                all_job_ids.append(job_id)

        logger.info("--- PHASE 1B: WAIT FOR ALL JOBS TO COMPLETE ---")
        polling_tasks = [poll_for_job_completion(job_id) for job_id in all_job_ids]
        completed_jobs = await asyncio.gather(*polling_tasks)

        logger.info("--- PHASE 1C: COLLECT CHUNK UUIDS ---")
        all_chunk_uuids = {}
        for job_doc in completed_jobs:
            user_id = job_doc.get("user_id")
            if user_id not in all_chunk_uuids:
                all_chunk_uuids[user_id] = []

            job_chunks = job_doc.get("chunk_uuids", [])
            if job_chunks:
                all_chunk_uuids[user_id].extend(job_chunks)
            logger.info(
                f"Collected {len(job_chunks)} chunk UUIDs for job {job_doc['id']} (tenant: {user_id})"
            )

        logger.success("--- All ingestion jobs complete. ---")

        # Wait for data to be indexed using readiness probes, but tolerate timeouts.
        logger.info("--- PHASE 1D: VERIFY DATA WITH READINESS PROBE ---")
        probe_tasks = []
        for user_id, chunk_ids in all_chunk_uuids.items():
            unique_chunk_ids = list(set(chunk_ids))
            if unique_chunk_ids:
                logger.info(f"Probing for {len(unique_chunk_ids)} chunks for tenant {user_id}...")
                probe_tasks.append(
                    readiness_probe(
                        collection_name,
                        user_id,
                        unique_chunk_ids,
                        ["weaviate", "graphrag"],
                        timeout_seconds=120,
                        success_threshold=1.0,  # For tests, we expect 100% success
                    )
                )

        probe_results = await asyncio.gather(*probe_tasks, return_exceptions=True)

        failed_probes = []
        for i, res in enumerate(probe_results):
            if res is not True:
                failed_probes.append(f"Probe {i + 1} failed with result: {res}")

        if failed_probes:
            logger.error(f"{len(failed_probes)} readiness probe(s) failed:")
            for failure in failed_probes:
                logger.error(f"  - {failure}")
            raise RuntimeError("Readiness probe phase did not succeed.")

        logger.success("--- All readiness probes passed successfully! ---")

        # ===================================================================
        # PHASE 2: DATABASE STATE VERIFICATION
        # ===================================================================
        logger.info("--- PHASE 2: DATABASE STATE VERIFICATION ---")

        # Check Weaviate
        tenant_a_weaviate_count = await count_weaviate_objects(
            collection_name, str(TENANT_A_DATA["user"].id)
        )
        tenant_b_weaviate_count = await count_weaviate_objects(
            collection_name, str(TENANT_B_DATA["user"].id)
        )
        assert tenant_a_weaviate_count > 0, "FAIL: No objects found in Weaviate for Tenant A"
        assert tenant_b_weaviate_count > 0, "FAIL: No objects found in Weaviate for Tenant B"
        logger.success(
            f"PASS: Weaviate ingestion verified (Tenant A: {tenant_a_weaviate_count} objs, Tenant B: {tenant_b_weaviate_count} objs)."
        )

        # Check Neo4j
        tenant_a_neo4j_count = await count_neo4j_nodes(str(TENANT_A_DATA["user"].id))
        tenant_b_neo4j_count = await count_neo4j_nodes(str(TENANT_B_DATA["user"].id))
        assert tenant_a_neo4j_count > 0, "FAIL: No chunk nodes found in Neo4j for Tenant A"
        assert tenant_b_neo4j_count > 0, "FAIL: No chunk nodes found in Neo4j for Tenant B"
        logger.success(
            f"PASS: Neo4j ingestion verified (Tenant A: {tenant_a_neo4j_count} nodes, Tenant B: {tenant_b_neo4j_count} nodes)."
        )

        logger.success("--- PHASE 2 COMPLETE: Database state verified. ---")

        # ===================================================================
        # PHASE 3: END-TO-END RAG QUERY AND ISOLATION CHECK
        # ===================================================================
        logger.info("--- PHASE 3: E2E RAG QUERY & ISOLATION CHECK ---")

        # This part may fail if the UUID issue is not yet resolved, which is acceptable for this test.
        # The primary goal is to validate the multi-tenancy creation and detection.
        logger.warning(
            "Proceeding to RAG query phase. This may fail if UUID mismatch issue is present."
        )

        for tenant_name, tenant_data in [("A", TENANT_A_DATA), ("B", TENANT_B_DATA)]:
            user = tenant_data["user"]
            logger.info(f"Running RAG query for Tenant {tenant_name} ({user.id})...")

            action_config = GraphRAGQueryConfig(
                collection_name=collection_name, query=tenant_data["query"]
            )
            action = ActionGraphRAGQuery(action_config=action_config, user=user)

            action_input = DataBlock(text=tenant_data["query"])
            # Create a mock token and context, just like in the pytest test
            expire = datetime.utcnow() + timedelta(minutes=5)
            to_encode = {"sub": str(user.id), "exp": int(expire.timestamp())}
            mock_token = jwt.encode(
                to_encode,
                SettingsProvider.sget().JWT_SECRET_KEY,
                algorithm=SettingsProvider.sget().JWT_ALGORITHM,
            )
            user.access_token = mock_token
            ctx = Context(user_id=str(user.id))

            result = await action.run_action(ctx=ctx, action_input=action_input)

            assert isinstance(result, DataBlocks), (
                f"Action failed for Tenant {tenant_name}: {result}"
            )
            final_answer = result.blocks[0].text.lower()

            logger.info(f"Tenant {tenant_name} Query: {tenant_data['query']}")
            logger.info(f"Tenant {tenant_name} Answer: {final_answer}")

            # --- ASSERT ISOLATION ---
            # Check that the answer contains the expected keywords for this tenant
            for keyword in tenant_data["expected_keywords"]:
                assert keyword.lower() in final_answer, (
                    f"FAIL: Tenant {tenant_name}'s answer was missing expected keyword '{keyword}'"
                )

            # Check that the answer DOES NOT contain keywords from the other tenant
            for keyword in tenant_data["forbidden_keywords"]:
                assert keyword.lower() not in final_answer, (
                    f"FAIL: Tenant {tenant_name}'s answer contained a forbidden keyword '{keyword}', indicating data leakage."
                )

            logger.success(
                f"PASS: Tenant {tenant_name}'s query was answered correctly and showed proper data isolation."
            )

        logger.success("--- PHASE 3 COMPLETE: Multi-tenant RAG queries successful. ---")

    except Exception as e:
        logger.exception("--- E2E Programmatic Multi-Tenancy Test FAILED ---")
        raise e
    finally:
        # ===================================================================
        # PHASE 4: CLEANUP
        # ===================================================================
        logger.info("--- PHASE 4: CLEANUP ---")

        # Cleanup S3
        settings = SettingsProvider.sget()
        s3_root_client = get_aws_s3(
            settings.AWS_S3_BUCKET_REGION,
            settings.AWS_ACCESS_KEY_ID,
            settings.AWS_SECRET_ACCESS_KEY,
            settings.AWS_S3_PUBLIC_BUCKET_NAME,
        )
        for prefix in s3_prefixes_to_clean:
            await s3_root_client.delete_prefix(prefix)
            logger.info(f"Cleaned up S3 prefix: {prefix}")

        # Cleanup Weaviate by deleting the entire collection
        weaviate_client = WeaviateVectorDBClient(
            url=settings.WEAVIATE_URL, api_key=settings.WEAVIATE_API_KEY, skip_init_checks=True
        )
        await weaviate_client.delete_class(collection_name)
        await weaviate_client.close()
        logger.info(f"Cleaned up Weaviate collection: {collection_name}")

        # Cleanup Neo4j
        if Neo4jManager._instance:
            for tenant_data in [TENANT_A_DATA, TENANT_B_DATA]:
                await asyncio.to_thread(
                    Neo4jManager._instance.delete_all_tenant_data, str(tenant_data["user"].id)
                )
            logger.info("Cleaned up Neo4j tenant data.")
            Neo4jManager.shutdown()

        # Cleanup local files
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
            logger.info(f"Removed temporary directory: {temp_dir}")


async def main():
    setup_logger()
    SettingsProvider.set_env_file(".env.local")
    SettingsProvider.set()
    settings = SettingsProvider.sget()

    from src.autobots.core.database.mongo_base import close_mongo_client, get_mongo_client

    mongo_client = None
    try:
        mongo_client = get_mongo_client()
        db = mongo_client[settings.MONGO_DATABASE]
        await run_programmatic_mt_e2e_test(db)
    finally:
        if mongo_client:
            close_mongo_client()

    logger.success("--- E2E Programmatic Multi-Tenancy Test PASSED SUCCESSFULLY ---")


if __name__ == "__main__":
    asyncio.run(main())
