# src/autobots/ingestion/worker.py

import asyncio
import json
import random
import uuid

from aiohttp import ClientResponseError
from src.autobots.data_model.app_state import AppState
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.core.database.mongo_base import get_mongo_db

# --- Core Application Imports ---
from src.autobots.core.redis.async_pool import get_async_redis
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.ingestion.exceptions import RetryableIngestionError
from src.autobots.ingestion.job_status import BackendState, JobState, JobStatusCRUD, JobStatusUpdate
from src.autobots.ingestion.polling import readiness_probe
from src.autobots.ingestion.s3_utils import cleanup_staging_prefix

# --- Service and Client Imports ---
from src.autobots.ingestion.service import IngestionService
from src.autobots.user.user import User
from src.autobots.user.user_orm_model import UserORM

MAX_CONCURRENT_JOBS = 10  # Configurable limit


async def _execute_and_log_job(
    job_data: dict, ctx: Context, app_state: AppState, user: UserORM, db: AsyncIOMotorDatabase, job_crud: JobStatusCRUD
):
    """
    Executes a single ingestion job with an internal retry loop.

    This function now owns the entire lifecycle of the job, including inline probing,
    and sets a definitive terminal state (AVAILABLE, PARTIAL_AVAILABLE, or FAILED).

    It will retry the entire job on transient errors, but once the mandatory Weaviate
    ingestion succeeds and is verified, the loop terminates to prevent duplicates.
    """
    job_id = job_data["job_id"]
    max_attempts = 3  # pull from settings later if desired
    base_backoff = 10  # seconds
    store_from_ingest = None  # Initialize outside the try block for visibility in finally

    try:
        for attempt in range(1, max_attempts + 1):
            try:
                # Determine effective tenant for this job. Backwards compatible:
                #   - Old jobs have no kb_id/tenant_id -> tenant_id falls back to user.id
                #   - New jobs may set kb_id and/or tenant_id explicitly.
                # Do NOT fall back to kb_id; tenant mapping may evolve independently of kb_id.
                tenant_id = job_data.get("tenant_id") or str(user.id)

                ingestion_service = IngestionService(
                    user_id=str(user.id),
                    tenant_id=tenant_id,
                )

                # The service now returns the live store instance
                ingestion_result = await ingestion_service._execute_ingestion_job(
                    s3_key=job_data["s3_key"],
                    original_filename=job_data["original_filename"],
                    collection_name=job_data["collection_name"],
                    db=db,
                    targets=job_data.get("targets", ["weaviate", "graphrag"]),
                    user=user,
                    ctx=ctx,
                    app_state=app_state,
                    job_data=job_data,  # Pass the full job dictionary for URL resolution
                )

                # Get the store for probing and later cleanup
                store_from_ingest = ingestion_result.get("store")
                # Unpack results from the sequential ingestion service
                chunk_uuids = ingestion_result.get("processed_data", {}).get("chunk_uuids", [])
                errors = ingestion_result.get("errors", [])

                # --- Guard for Zero-Chunk Files ---
                # If a file is valid but produces no content, treat it as a success (e.g., empty files).
                if not chunk_uuids:
                    orig_filename = job_data.get("original_filename", "<unknown>")
                    logger.bind(ctx=ctx).info(
                        f"Job {job_id} for '{orig_filename}' produced no chunks. Marking as complete."
                    )
                    await job_crud.update_by_id(
                        job_id,
                        JobStatusUpdate(
                            status=JobState.AVAILABLE,
                            weaviate_status=BackendState.SKIPPED,
                            graphrag_status=BackendState.SKIPPED,
                        ),
                        str(user.id),
                    )
                    return  # Exit successfully

                # Safely get the client for the probe
                weaviate_client_from_ingest = (
                    store_from_ingest._client if store_from_ingest else None
                )

                # --- Probing and Final Status Update ---
                weaviate_ready = False
                graph_ready = False
                targets_requested = set(job_data.get("targets", ["weaviate", "graphrag"]))

                # 1. Probe Weaviate if it was a target, and hard-fail the job if the probe fails.
                if "weaviate" in targets_requested:
                    weaviate_ready = await readiness_probe(
                        collection_name=job_data["collection_name"],
                        user_id=tenant_id,
                        chunk_uuids=chunk_uuids,
                        targets=["weaviate"],
                        existing_weaviate_client=weaviate_client_from_ingest,
                        success_threshold=1.0,
                    )
                    # If Weaviate was requested and its probe fails, the entire job is a failure.
                    if not weaviate_ready:
                        errors.append(
                            "FATAL: Weaviate readiness probe failed. Data is not queryable."
                        )
                        await job_crud.update_by_id(
                            job_id,
                            JobStatusUpdate(
                                status=JobState.FAILED,
                                weaviate_status=BackendState.FAILED,
                                graphrag_status=BackendState.FAILED
                                if "graphrag" in targets_requested
                                else BackendState.SKIPPED,
                                error_message="; ".join(errors),
                            ),
                            str(user.id),
                        )
                        return  # HARD STOP.

                # 2. Probe GraphRAG if it was a target.
                if "graphrag" in targets_requested:
                    graph_ready = await readiness_probe(
                        collection_name=job_data["collection_name"],
                        user_id=tenant_id,
                        chunk_uuids=chunk_uuids,
                        targets=["graphrag"],
                        # Reuse the same gRPC channel that performed the write
                        existing_weaviate_client=weaviate_client_from_ingest,
                        success_threshold=0.75,
                    )
                    if not graph_ready:
                        errors.append(
                            "GraphRAG readiness probe failed or did not meet the success threshold."
                        )

                # --- Determine Final Backend and Overall Status ---
                weaviate_status = (
                    BackendState.SKIPPED
                    if "weaviate" not in targets_requested
                    else (BackendState.AVAILABLE if weaviate_ready else BackendState.FAILED)
                )

                graphrag_status = (
                    BackendState.SKIPPED
                    if "graphrag" not in targets_requested
                    else (BackendState.AVAILABLE if graph_ready else BackendState.FAILED)
                )

                if graphrag_status in (BackendState.AVAILABLE, BackendState.SKIPPED):
                    overall_status = JobState.AVAILABLE
                else:  # Weaviate is OK (guaranteed by hard stop), but GraphRAG failed.
                    overall_status = JobState.PARTIAL_AVAILABLE

                # Update the job document ONCE with the final, complete state.
                await job_crud.update_by_id(
                    job_id,
                    JobStatusUpdate(
                        status=overall_status,
                        weaviate_status=weaviate_status,
                        graphrag_status=graphrag_status,
                        chunk_uuids=chunk_uuids,
                        error_message="; ".join(errors) or None,
                    ),
                    str(user.id),
                )
                logger.bind(ctx=ctx).success(
                    f"Job {job_id} completed with final status: {overall_status.value}"
                )
                return  # Exit retry loop on any terminal state

            # ────────── retryable errors (S3 403/404 or empty chunks) ──────────
            except (RetryableIngestionError, ClientResponseError) as exc:
                # If it's an HTTP error make sure it is the 403/404 family.
                if isinstance(exc, ClientResponseError) and exc.status not in (403, 404):
                    # treat other HTTP codes as unrecoverable
                    raise

                if attempt == max_attempts:
                    await job_crud.update_by_id(
                        job_id,
                        JobStatusUpdate(
                            status=JobState.FAILED,
                            error_message=str(exc),
                            weaviate_status=BackendState.FAILED,
                        ),
                        str(user.id),
                    )
                    logger.bind(ctx=ctx).error(
                        f"Job {job_id} FAILED after {max_attempts} attempts due to: {exc}"
                    )
                    return

                wait = base_backoff * (2 ** (attempt - 1)) + random.uniform(0, 1)
                logger.bind(ctx=ctx).warning(
                    f"Retryable error on attempt {attempt}/{max_attempts} for job {job_id}: "
                    f"{exc}. Retrying in {wait:.1f}s."
                )
                await job_crud.update_by_id(
                    job_id,
                    JobStatusUpdate(error_message=str(exc)),  # Update error, keep status PROCESSING
                    str(user.id),
                )
                await asyncio.sleep(wait)
                continue  # Explicitly continue to next iteration of the loop

    # ────────── unrecoverable errors – fail fast ──────────
    except Exception as exc:
        # Log the full traceback for unrecoverable errors to aid debugging.
        logger.bind(ctx=ctx).exception(f"Job {job_id} FAILED with unrecoverable error.")
        await job_crud.update_by_id(
            job_id,
            JobStatusUpdate(
                status=JobState.FAILED,
                error_message=str(exc),
                weaviate_status=BackendState.FAILED,
                graphrag_status=BackendState.FAILED,
            ),
            str(user.id),
        )
        return
    finally:
        # --- FIX: Ensure the store is always closed after job completion or failure ---
        if store_from_ingest:
            try:
                await store_from_ingest.close()
            except Exception as e:
                logger.warning(
                    f"Failed to close WeaviateStore during final cleanup for job {job_id}: {e}"
                )


async def process_single_job(job_string: str, queue_name: str, db: AsyncIOMotorDatabase):
    """
    A highly resilient function to process a single job. It ensures that the
    staging area is cleaned up regardless of success or failure.
    """
    job_data = None
    try:
        job_data = json.loads(job_string)
    except json.JSONDecodeError as e:
        logger.error(
            f"FATAL: Could not parse job JSON from queue '{queue_name}'. Job: '{job_string}'. Error: {e}"
        )
        # Cannot clean up S3 if we can't parse the key, so we just log and exit.
        return

    # <<< DEBUG LOG: Verify the received job data >>>
    logger.bind(job_data=job_data).debug("Worker received and parsed job data from Redis")

    # Create the context for this specific job
    ctx = Context(user_id=job_data.get("user_id"), trace_id=f"ingest-{uuid.uuid4().hex[:8]}")
    app_state = AppState()
    logger.bind(ctx=ctx).info(f"Worker picked up job from queue '{queue_name}'")

    user_id = job_data.get("user_id")

    # Hydrate the user object, falling back to a minimal stub if the DB lookup fails.
    try:
        user_stub = UserORM(id=user_id)
        user_doc = await User.get(ctx=ctx, user=user_stub, id=user_id, db=db)
        if not user_doc:
            raise ValueError("User.get returned None")
        user = UserORM(**user_doc.model_dump())
        logger.bind(ctx=ctx).debug(f"Hydrated user {user.id} with email: {user.email}")
    except Exception as e:
        logger.bind(ctx=ctx).warning(f"User hydration failed for {user_id}: {e}. Using stub.")
        user = UserORM(id=user_id)

    job_crud = JobStatusCRUD(db)

    # Set initial PROCESSING state for all relevant backends for better observability
    graphrag_initial_state = (
        BackendState.PROCESSING
        if "graphrag" in job_data.get("targets", [])
        else BackendState.SKIPPED
    )
    await job_crud.update_by_id(
        job_data["job_id"],
        JobStatusUpdate(
            status=JobState.PROCESSING,
            weaviate_status=BackendState.PROCESSING,
            graphrag_status=graphrag_initial_state,
        ),
        job_data["user_id"],
    )

    try:
        await _execute_and_log_job(job_data, ctx, app_state, user, db, job_crud)
    except Exception:
        # The inner function now re-raises, so we catch it here to prevent the worker from crashing.
        # The inner function has already logged the error and updated the DB status.
        logger.bind(ctx=ctx).error("Job execution failed. Cleanup will proceed.")
    finally:
        # This cleanup block ALWAYS runs, even if _execute_and_log_job fails.
        if job_data and job_data.get("s3_key"):
            # This function is hardcoded to the staging prefix.
            await cleanup_staging_prefix(job_data["s3_key"])


async def process_with_semaphore(
    semaphore, job_string: str, queue_name: str, db: AsyncIOMotorDatabase
):
    """Wrapper to ensure the semaphore is released even if the job fails."""
    async with semaphore:
        await process_single_job(job_string, queue_name, db)


async def main_worker_loop():
    """
    The main loop for a dedicated worker process. It efficiently and resiliently
    polls Redis for new ingestion jobs from both high and low priority queues.

    TODO: Consider migrating to an async Redis client (e.g., redis-py's async support)
          if worker throughput becomes a bottleneck.
    """
    logger.info(f"Ingestion worker (Semaphore) started with max concurrency: {MAX_CONCURRENT_JOBS}")
    redis_client = await get_async_redis()
    db = next(get_mongo_db())
    settings = SettingsProvider.sget()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
    client = db.client  # Get the underlying client for safe closing.

    try:
        while True:
            try:
                job_data_raw = await redis_client.brpop(
                    [
                        settings.REDIS_INGESTION_HIGH_PRIORITY_QUEUE,
                        settings.REDIS_INGESTION_LOW_PRIORITY_QUEUE,
                    ],
                    timeout=5,
                )

                if job_data_raw:
                    queue_name_bytes, job_string_bytes = job_data_raw
                    queue_name = queue_name_bytes.decode("utf-8")
                    job_string = job_string_bytes.decode("utf-8")

                    # The main loop can spawn tasks quickly, but the tasks themselves
                    # will wait at `async with semaphore:` until a permit is available.
                    asyncio.create_task(
                        process_with_semaphore(semaphore, job_string, queue_name, db)
                    )

            except ConnectionError as e:
                logger.error(f"Worker could not connect to Redis: {e}. Retrying in 10 seconds...")
                await asyncio.sleep(10)
            except Exception as e:
                # This is a catch-all for unexpected errors in the main loop itself.
                logger.exception(
                    f"An unexpected error occurred in the main worker loop: {e}. Continuing..."
                )
                await asyncio.sleep(1)
    finally:
        # This block will run when the worker process is terminated, ensuring cleanup.
        client.close()
        logger.info("MongoDB client for worker loop has been closed.")
