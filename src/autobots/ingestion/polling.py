# src/autobots/ingestion/polling.py

import asyncio
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import weaviate.classes as wvc  # Import for filter
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from weaviate.exceptions import WeaviateBaseError, WeaviateQueryError

from src.autobots.conn.weaviate.weaviate_client import WeaviateVectorDBClient
from src.autobots.core.settings import SettingsProvider
from src.autobots.ingestion.job_status import BackendState, JobState, JobStatusCRUD
from src.autobots.ingestion.neo4j_loader import Neo4jManager


async def _get_loop_local_db():
    """Creates a new, non-cached Motor client attached to the current event loop."""
    settings = SettingsProvider.sget()
    client = AsyncIOMotorClient(settings.MONGO_CONN)
    return client, client[settings.MONGO_DATABASE]


async def readiness_probe(
    collection_name: str,
    user_id: str,
    chunk_uuids: List[str],  # Use worker-reported UUIDs directly
    targets: List[str] = ["weaviate", "graphrag"],
    existing_weaviate_client: Optional[WeaviateVectorDBClient] = None,
    timeout_seconds: int = 120,
    success_threshold: float = 0.75,
) -> bool:
    """
    [V2 Refactored] Polls backends to ensure a sufficient percentage of specific
    chunks from an ingestion job are available and queryable. This probe is robust
    as it checks for the existence of *specific UUIDs* rather than a total count.

    Args:
        collection_name: The name of the Weaviate collection.
        user_id: The ID of the tenant.
        chunk_uuids: The list of specific chunk UUIDs to verify.
        targets: The backends to probe (e.g., ['weaviate', 'graphrag']).
        existing_weaviate_client: An optional, live client to reuse, avoiding
            consistency issues.
        timeout_seconds: Maximum time to wait for the data to become available.
        success_threshold: The fraction of UUIDs that must be found for the probe to
                           succeed (e.g., 1.0 for 100%, 0.75 for 75%).
    """
    # De-duplicate UUIDs and handle the zero-chunk edge case.
    unique_chunk_uuids = list(set(chunk_uuids))
    if not unique_chunk_uuids:
        logger.info("Readiness probe called with no chunk UUIDs. Returning success.")
        return True

    expected_count = len(unique_chunk_uuids)
    required_count = max(1, int(expected_count * success_threshold))
    start_time = time.time()

    async def _probe_weaviate():
        client_to_use = existing_weaviate_client
        was_client_provided = client_to_use is not None

        if client_to_use is None:
            settings = SettingsProvider.sget()
            client_to_use = WeaviateVectorDBClient(
                url=settings.WEAVIATE_URL, api_key=settings.WEAVIATE_API_KEY, skip_init_checks=True
            )
        try:
            while time.time() - start_time < timeout_seconds:
                try:
                    # KEEP UUID-SPECIFIC LOGIC: Check for the exact UUIDs from this job
                    collection = client_to_use.client.collections.get(collection_name).with_tenant(
                        user_id
                    )
                    WEAVIATE_BATCH_SIZE = 100
                    total_found = 0
                    for i in range(0, len(unique_chunk_uuids), WEAVIATE_BATCH_SIZE):
                        batch = unique_chunk_uuids[i : i + WEAVIATE_BATCH_SIZE]
                        # Try HTTP query first, fall back to gRPC if needed
                        try:
                            response = await asyncio.to_thread(
                                collection.query.fetch_objects,
                                filters=wvc.query.Filter.by_id().contains_any(batch),
                                limit=len(batch),
                                return_properties=[],
                            )
                        except Exception as e:
                            if "tenant not found" in str(e).lower():
                                elapsed = time.time() - start_time
                                logger.debug(
                                    f"Weaviate probe (elapsed {elapsed:.1f}s) got transient error '{str(e).lower()}'"
                                )
                                break  # Exit inner loop to retry
                            continue  # Skip this batch on other errors
                        total_found += len(response.objects)

                    if total_found >= required_count:
                        logger.info(
                            f"Weaviate probe successful for tenant {user_id}. Found {total_found}/{expected_count} chunks (required: {required_count})."
                        )
                        return True
                    else:
                        elapsed = time.time() - start_time
                        logger.debug(
                            f"Weaviate probe (elapsed {elapsed:.1f}s): found {total_found}/{expected_count} chunks, retrying..."
                        )

                except (WeaviateQueryError, WeaviateBaseError) as e:
                    msg = str(e).lower()
                    if any(
                        s in msg
                        for s in [
                            "tenant not found",
                            "collection not found",
                            "class not found",
                            "could not find class",  # gRPC error for non-existent collection
                            "non-existing index",
                        ]
                    ):
                        elapsed = time.time() - start_time
                        logger.debug(
                            f"Weaviate probe (elapsed {elapsed:.1f}s) got transient error '{msg}', retrying..."
                        )
                    else:
                        logger.exception(f"Weaviate probe failed with unexpected error: {e}")
                        raise
                await asyncio.sleep(2)
        finally:
            if not was_client_provided and client_to_use:
                await client_to_use.close()
        logger.error(
            f"Weaviate readiness probe timed out after {timeout_seconds}s for tenant '{user_id}'. Required {required_count} chunks, but conditions not met."
        )
        return False

    async def _probe_neo4j():
        client = await Neo4jManager.get_client()
        # UUID-SPECIFIC LOGIC: Query for the exact chunk IDs from this job
        query = "MATCH (n:Chunk) WHERE n.tenantId = $tenantId AND n.id IN $chunkIds RETURN count(n) as count"
        params = {"tenantId": user_id, "chunkIds": unique_chunk_uuids}

        # Track retries to prevent infinite loops on empty graphs
        retry_count = 0
        max_retries = 60  # 120 seconds / 2 second sleep

        while time.time() - start_time < timeout_seconds:
            try:
                result = await asyncio.to_thread(client.query, query, user_id, params=params)
                found_count = result[0]["count"] if result and result[0] else 0
                if found_count >= required_count:
                    logger.info(
                        f"Neo4j probe successful for tenant {user_id}. Found {found_count}/{expected_count} chunks (required: {required_count})."
                    )
                    return True

                # Prevent infinite loops when graph is legitimately empty
                retry_count += 1
                if retry_count >= max_retries and found_count == 0:
                    logger.warning(
                        f"Neo4j probe giving up after {retry_count} retries with 0 chunks found. Graph may be empty due to processing failures."
                    )
                    return False

                else:
                    elapsed = time.time() - start_time
                    logger.debug(
                        f"Neo4j probe (elapsed {elapsed:.1f}s): found {found_count}/{expected_count} chunks, retrying..."
                    )
            except Exception as e:
                elapsed = time.time() - start_time
                logger.warning(
                    f"Neo4j probe (elapsed {elapsed:.1f}s) encountered a transient error, retrying... Error: {e}"
                )
            await asyncio.sleep(2)  # Use a slightly longer sleep
        logger.error(
            f"Neo4j readiness probe timed out after {timeout_seconds}s for tenant '{user_id}'. Required {required_count} chunks, but conditions not met."
        )
        return False

    probes = []
    if "weaviate" in targets:
        probes.append(_probe_weaviate())
    if "graphrag" in targets:
        probes.append(_probe_neo4j())

    # Use return_exceptions=True to prevent one failure from canceling others.
    results = await asyncio.gather(*probes, return_exceptions=True)
    return all(res is True for res in results)


async def poll_for_job_completion(
    job_id: str,
    db: Optional[AsyncIOMotorDatabase] = None,
    timeout_seconds: int = 240,
    final_states: Optional[List[JobState]] = None,
    required_backends: Optional[List[str]] = None,
) -> Dict:
    """
    Polls the jobs collection for a specific final status.
    Args:
        db: An optional, existing database connection. If not provided, a temporary
            one will be created for the duration of the poll.
        job_id: The ID of the job to poll.
        timeout_seconds: Maximum time to wait.
        final_states: A list of states that will terminate the polling.
        required_backends: An optional list of backend names that must be AVAILABLE
            before considering the job complete.
    """
    final_states = final_states or [JobState.AVAILABLE, JobState.PARTIAL_AVAILABLE, JobState.FAILED]
    start_time = time.time()

    db_to_use = db
    client_to_close = None
    if db_to_use is None:
        client_to_close, db_to_use = await _get_loop_local_db()

    job_crud = JobStatusCRUD(db_to_use)
    try:
        while time.time() - start_time < timeout_seconds:
            job_doc = await job_crud.get_by_id(job_id)
            if job_doc and JobState(job_doc.status) in final_states:
                # --- STALE JOB PROTECTION: Verify this is a recent job ---
                job_age = datetime.utcnow() - job_doc.created_at
                if job_age > timedelta(minutes=10):
                    logger.warning(
                        f"Job {job_id} is suspiciously old ({job_age}), continuing to wait for fresh update"
                    )
                    await asyncio.sleep(2)
                    continue

                if required_backends:
                    backends_ready = True
                    for backend in required_backends:
                        backend_status_attr = f"{backend}_status"
                        raw_status = getattr(job_doc, backend_status_attr, None)

                        # Handle None/missing status explicitly
                        if raw_status is None:
                            backends_ready = False
                            elapsed = time.time() - start_time
                            logger.debug(
                                f"Job {job_id} waiting for backend '{backend}' (status: None/missing) - elapsed: {elapsed:.1f}s"
                            )
                            break

                        # Convert to enum safely
                        try:
                            backend_status = (
                                raw_status
                                if isinstance(raw_status, BackendState)
                                else BackendState(raw_status)
                            )
                        except (ValueError, TypeError):
                            backends_ready = False
                            elapsed = time.time() - start_time
                            logger.debug(
                                f"Job {job_id} backend '{backend}' has invalid status '{raw_status}' - elapsed: {elapsed:.1f}s"
                            )
                            break

                        # Only AVAILABLE is acceptable for required backends
                        if backend_status is not BackendState.AVAILABLE:
                            backends_ready = False
                            elapsed = time.time() - start_time
                            logger.debug(
                                f"Job {job_id} waiting for backend '{backend}' (status: {backend_status}) - elapsed: {elapsed:.1f}s"
                            )
                            break

                    if backends_ready:
                        logger.info(
                            f"Job {job_id} reached final state '{job_doc.status}' with all required backends ready."
                        )
                        return job_doc.model_dump()
                else:
                    logger.info(f"Job {job_id} reached final state: {job_doc.status}")
                    return job_doc.model_dump()
            await asyncio.sleep(2)
    finally:
        if client_to_close:
            client_to_close.close()

    raise TimeoutError(f"Polling for job {job_id} timed out after {timeout_seconds} seconds.")
