"""MongoDB CRUD operations for data_plane_v2 job state (AUT-1378)."""

from datetime import datetime, timezone
from typing import Any

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from src.autobots.action.action_type.action_text2text.action_data_plane.data_plane_v2_doc_model import (
    CHUNK_METRICS_COLLECTION,
    COLLECTION,
    ChunkMetric,
    DataPlaneError,
    DataPlaneJob,
    JobCheckpoint,
    JobMetrics,
    JobStatus,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_data_plane_indexes(db: AsyncIOMotorDatabase) -> None:
    """Idempotent index creation — call once at startup."""
    try:
        await db[COLLECTION].create_index(
            [("user_id", 1), ("job_id", 1)],
            name="dp_user_job_idx",
            unique=True,
            background=True,
        )
        await db[COLLECTION].create_index(
            [("user_id", 1), ("idempotency_key", 1)],
            name="dp_user_idem_idx",
            background=True,
        )
        await db[COLLECTION].create_index(
            [("user_id", 1), ("job_name", 1), ("updated_at", -1)],
            name="dp_user_jobname_idx",
            background=True,
        )
        await db[COLLECTION].create_index(
            [("user_id", 1), ("status", 1), ("updated_at", -1)],
            name="dp_user_status_idx",
            background=True,
        )
        await db[CHUNK_METRICS_COLLECTION].create_index(
            [("job_id", 1), ("shard_index", 1), ("chunk_index", 1)],
            name="dp_chunk_job_shard_chunk_idx",
            background=True,
        )
        await db[CHUNK_METRICS_COLLECTION].create_index(
            [("user_id", 1), ("job_id", 1)],
            name="dp_chunk_user_job_idx",
            background=True,
        )
    except Exception as e:
        logger.error(f"data_plane_v2 ensure_indexes failed: {e}")


async def create_job(
    db: AsyncIOMotorDatabase,
    user_id: str,
    mode: str,
    connection_name: str,
    job_name: str | None,
    idempotency_key: str | None,
    config_dict: dict,
    table: str | None = None,
) -> DataPlaneJob:
    job = DataPlaneJob(
        user_id=user_id,
        mode=mode,
        connection_name=connection_name,
        job_name=job_name,
        idempotency_key=idempotency_key,
        config=config_dict,
        checkpoint=JobCheckpoint(table=table),
    )
    await db[COLLECTION].insert_one(job.model_dump())
    return job


async def get_job(
    db: AsyncIOMotorDatabase,
    user_id: str,
    job_id: str | None = None,
    job_name: str | None = None,
    idempotency_key: str | None = None,
) -> DataPlaneJob | None:
    query: dict[str, Any] = {"user_id": user_id}
    if job_id:
        query["job_id"] = job_id
    elif idempotency_key:
        query["idempotency_key"] = idempotency_key
    elif job_name:
        query["job_name"] = job_name
    else:
        return None

    doc = await db[COLLECTION].find_one(query, sort=[("created_at", -1)])
    if not doc:
        return None
    doc.pop("_id", None)
    return DataPlaneJob(**doc)


async def set_status(
    db: AsyncIOMotorDatabase,
    job_id: str,
    user_id: str,
    status: JobStatus,
) -> None:
    result = await db[COLLECTION].update_one(
        {"job_id": job_id, "user_id": user_id},
        {"$set": {"status": status.value, "updated_at": _now()}},
    )
    if result.matched_count == 0:
        logger.warning(
            f"data_plane_v2 set_status: no document matched job_id={job_id} user_id={user_id}"
        )


async def claim_job_for_resume(
    db: AsyncIOMotorDatabase,
    job_id: str,
    user_id: str,
) -> DataPlaneJob | None:
    """Atomically transition a partial/failed job to running and return the updated doc.

    Returns None if the job does not exist, is already running, or is cancelled —
    preventing concurrent duplicate resumes without explicit locking.
    """
    doc = await db[COLLECTION].find_one_and_update(
        {
            "job_id": job_id,
            "user_id": user_id,
            "status": {"$in": [JobStatus.partial.value, JobStatus.failed.value]},
        },
        {"$set": {"status": JobStatus.running.value, "updated_at": _now()}},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        return None
    doc.pop("_id", None)
    return DataPlaneJob(**doc)


async def is_job_cancelled(
    db: AsyncIOMotorDatabase,
    job_id: str,
    user_id: str,
) -> bool:
    """Lightweight poll — called between shards to detect a user-initiated cancel."""
    doc = await db[COLLECTION].find_one(
        {"job_id": job_id, "user_id": user_id},
        projection={"status": 1},
    )
    return doc is not None and doc.get("status") == JobStatus.cancelled.value


async def checkpoint_shard(
    db: AsyncIOMotorDatabase,
    job_id: str,
    user_id: str,
    shard_idx: int,
    shard_ok: bool,
    metrics: JobMetrics,
    errors: list[DataPlaneError],
    chunk_metrics: list[ChunkMetric],
) -> None:
    """Persist progress after each shard.

    Only adds shard_idx to completed_shard_indices when shard_ok=True so that
    partially-failed shards are retried on resume rather than silently skipped.
    Chunk metrics are written to a separate collection to prevent the main job
    document from growing without bound.
    """
    update: dict[str, Any] = {
        "$set": {
            "checkpoint.current_shard_index": shard_idx,
            "checkpoint.rows_committed": metrics.rows_committed,
            "metrics": metrics.model_dump(),
            "errors": [e.model_dump() for e in errors],
            "updated_at": _now(),
        }
    }
    if shard_ok:
        update["$addToSet"] = {"checkpoint.completed_shard_indices": shard_idx}

    result = await db[COLLECTION].update_one(
        {"job_id": job_id, "user_id": user_id},
        update,
    )
    if result.matched_count == 0:
        logger.warning(
            f"data_plane_v2 checkpoint_shard: no document matched job_id={job_id} user_id={user_id}"
        )

    if chunk_metrics:
        try:
            await db[CHUNK_METRICS_COLLECTION].insert_many(
                [c.model_dump() for c in chunk_metrics],
                ordered=False,
            )
        except Exception as e:
            logger.warning(f"data_plane_v2 chunk_metrics insert failed (non-fatal): {e}")


async def finalize_job(
    db: AsyncIOMotorDatabase,
    job_id: str,
    user_id: str,
    status: JobStatus,
    metrics: JobMetrics,
    errors: list[DataPlaneError],
) -> None:
    error_summary = f"{len(errors)} error(s): {errors[0].message}" if errors else None
    result = await db[COLLECTION].update_one(
        {"job_id": job_id, "user_id": user_id},
        {
            "$set": {
                "status": status.value,
                "metrics": metrics.model_dump(),
                "errors": [e.model_dump() for e in errors],
                "error_summary": error_summary,
                "updated_at": _now(),
            }
        },
    )
    if result.matched_count == 0:
        logger.warning(
            f"data_plane_v2 finalize_job: no document matched job_id={job_id} user_id={user_id}"
        )
