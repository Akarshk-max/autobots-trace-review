import datetime
import json
import uuid
from typing import Any, Callable, Dict, List

from apscheduler.job import Job
from apscheduler.jobstores.mongodb import MongoDBJobStore
from apscheduler.schedulers import SchedulerNotRunningError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.util import undefined
from loguru import logger

from src.autobots.conn.object_storage.factory import get_public_storage
from src.autobots.core.database.mongo_base import get_mongo_db, get_pymongo_client
from src.autobots.core.redis.async_pool import get_async_redis
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.ingestion.job_status import (
    BackendState,
    JobState,
    JobStatusCreate,
    JobStatusCRUD,
    JobStatusUpdate,
)
from src.autobots.schedule.schedule_doc_model import CronSchedule
from src.autobots.schedule.trigger import CronTriggerModel, IntervalTriggerModel
from src.autobots.user.user_orm_model import UserORM

DEAD_LETTER_QUEUE = "ingestion:dead_letter_jobs"


# ==============================================================================
# --- DEVELOPER NOTE: Nightly Sync Configuration ---
# The behavior of the `run_nightly_s3_sync` job is controlled by environment
# variables configured in `src/autobots/core/settings.py`.
#
# - INGESTION_WORKER_S3_SYNC_MODE: Can be 'WHITELIST', 'ALL_USERS', or 'DISABLED'.
# - INGESTION_WORKER_S3_SYNC_WHITELIST_USER_IDS: A list of user IDs for 'WHITELIST' mode.
# - INGESTION_WORKER_S3_SYNC_BLACKLIST_USER_IDS: A list of user IDs to always exclude.
#


# Distributed lock using a static key for atomicity.
async def distributed_lock_wrapper(func: Callable, job_id: str, *args, **kwargs):
    """
    Ensures that only one instance of a scheduled job runs at a time
    across multiple schedulers in a distributed environment.
    """
    settings = SettingsProvider().sget()
    client = get_pymongo_client()
    db = client[settings.MONGO_DATABASE]
    locks_collection = db["scheduler_locks"]

    # Use a static lock ID per job_id for a true atomic check.
    lock_id = f"job_lock_{job_id}"
    lock_expiry_minutes = 5

    try:
        # Atomically attempt to acquire the lock.
        # This operation will only succeed if the document does not exist
        # or if the existing lock has expired.
        now = datetime.datetime.utcnow()
        result = locks_collection.find_one_and_update(
            {"_id": lock_id, "expires_at": {"$lt": now}},
            {"$set": {"expires_at": now + datetime.timedelta(minutes=lock_expiry_minutes)}},
            upsert=True,
        )

        # If result is None, it means we successfully created or updated the lock.
        if result is None:
            logger.info(f"Acquired lock for job {job_id}.")
            return await func(*args, **kwargs)
        else:
            logger.info(f"Job {job_id} is already locked by another worker.")
            return None
    except Exception as e:
        logger.exception(f"Error during distributed lock acquisition for job {job_id}: {e}")
    finally:
        # The lock will expire on its own. No need for explicit release in this model.
        # A separate cleanup job could remove very old, expired locks if necessary.
        pass


class Schedule:
    JOBSTORE = "default"  # 'default' is the default jobstore, if env var, then remove all jobstores before adding this one
    EXECUTOR = "default"
    _scheduler: AsyncIOScheduler | None = None

    def create_schedule(
        self,
        ctx: Context,
        func: Any,
        trigger: IntervalTriggerModel | CronTriggerModel,
        args: list | tuple = None,
        kwargs: dict = None,
        id: str = None,
        name: str = None,
        misfire_grace_time: int = undefined,
        coalesce: bool = True,
        max_instances: int = 1,
        next_run_time: datetime = undefined,
        jobstore: str = JOBSTORE,
        executor: str = EXECUTOR,
        replace_existing: bool = True,
    ) -> Job:
        # Create a partial function with the job_id
        # from functools import partial
        # wrapped_func = partial(distributed_lock_wrapper, func, id)
        trigger_dict = {}
        match trigger:
            case IntervalTriggerModel():
                trigger_dict = trigger.model_dump(exclude_none=True)
            case CronTriggerModel():
                trigger_dict = trigger.model_dump(exclude_none=True)

        job = Schedule._scheduler.add_job(
            func=func,
            **trigger_dict,
            args=args,
            kwargs=kwargs,
            id=id,
            name=name,
            misfire_grace_time=misfire_grace_time,
            coalesce=coalesce,
            max_instances=max_instances,
            next_run_time=next_run_time,
            jobstore=jobstore,
            executor=executor,
            replace_existing=replace_existing,
        )
        logger.bind(ctx=ctx).info(
            f"Created schedule: {job.id} {job.name} {job.next_run_time} {job.trigger} {job.args} {job.kwargs}"
        )
        return job

    def modify_schedule(self, ctx: Context, job_id: str, **changes) -> Job:
        job = Schedule._scheduler.modify_job(job_id=job_id, **changes)
        logger.bind(ctx=ctx).info(
            f"Modified schedule: {job.id} {job.name} {job.next_run_time} {job.trigger} {job.args} {job.kwargs}"
        )
        return job

    def get_schedule(self, job_id: str, jobstore: str = None) -> Job:
        job = Schedule._scheduler.get_job(job_id, jobstore)
        return job

    def list_schedule(self, jobstore: str = None) -> List[Job]:
        jobs = Schedule._scheduler.get_jobs(jobstore)
        return jobs

    def delete_schedule(self, job_id: str, jobstore: str = None) -> Any:
        job = Schedule._scheduler.remove_job(job_id, jobstore)
        return job

    @staticmethod
    def start_scheduler(
        collection_name: str = SettingsProvider.sget().SCHEDULE_JOBSTORE_MONGO_DB_COLLECTION_NAME,
    ):
        settings = SettingsProvider().sget()
        if Schedule._scheduler is None or not Schedule._scheduler.running:
            try:
                jobstores = {
                    # 'default' is the default jobstore, if env var, then remove all jobstores before adding this one
                    Schedule.JOBSTORE: MongoDBJobStore(
                        database=settings.MONGO_DATABASE,
                        collection=collection_name,
                        # APScheduler is only compatible with PyMongo library, lookout for future updates
                        client=get_pymongo_client(),
                        pickle_protocol=4,  # Add this to ensure compatibility
                        # Add these parameters for distributed locking
                        # Use a derived lock collection name to ensure lock isolation between schedulers
                        lock_collection=f"{collection_name}_locks",
                        lock_ttl=10,  # Time in seconds before a lock expires
                    )
                }
                job_defaults = {
                    "coalesce": True,  # Combine multiple pending executions into one
                    "max_instances": 1,  # Only one instance of each job can run at a time
                    "misfire_grace_time": None,  # Always run misfired jobs
                }
                Schedule._scheduler = AsyncIOScheduler(
                    jobstores=jobstores,
                    job_defaults=job_defaults,
                    # Add timezone awareness and process isolation
                    timezone="UTC",
                    # standalone=True  # This ensures each scheduler runs independently
                )
                Schedule._scheduler.start()
                logger.info(f"Scheduler started with job store collection: '{collection_name}'")
            except Exception as e:
                logger.exception(f"Error in start_scheduler: {str(e)}")
                raise

    @staticmethod
    def stop_scheduler():
        try:
            if Schedule._scheduler and Schedule._scheduler.running:
                Schedule._scheduler.shutdown(wait=True)
        except SchedulerNotRunningError as e:
            logger.warning(f"Scheduler was already stopped: {str(e)}")
        except Exception as e:
            logger.exception(f"Error in stop_scheduler: {str(e)}")
            raise

    @staticmethod
    def is_schedule_safe(ctx: Context, trigger: CronSchedule | IntervalTriggerModel) -> (bool, str):
        is_safe = False
        error = ""
        try:
            match trigger:
                case IntervalTriggerModel():
                    if IntervalTriggerModel.model_validate(trigger).seconds >= 1 * 60 * 60:
                        is_safe = True
                    else:
                        error = "Interval trigger must be at least 1 hour"
                case CronSchedule():
                    cron_trigger = CronSchedule.model_validate(trigger)
                    if cron_trigger:
                        is_safe = True
                    # if cron_trigger.minute:
                    #     if cron_trigger.hour is not None:
                    #         is_safe = True
                    #     else:
                    #         error = "Cron trigger must be more than a day"
                    # else:
                    #     is_safe = True
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in is_schedule_safe: {str(e)}")
        return is_safe, error


async def run_nightly_s3_sync():
    """
    A cron job that scans user folders in the PUBLIC S3 bucket, copies new files
    to a temporary staging area within the same bucket, and queues ingestion jobs.
    """
    ctx = Context(user_id="system_cron")
    settings = SettingsProvider.sget()

    # Early exit if disabled, providing a clear "kill switch".
    if settings.INGESTION_WORKER_S3_SYNC_MODE == "DISABLED":
        logger.bind(ctx=ctx).warning("Nightly S3 sync is DISABLED. Skipping execution.")
        return

    logger.bind(ctx=ctx).info(
        f"Starting nightly S3 sync job in '{settings.INGESTION_WORKER_S3_SYNC_MODE}' mode..."
    )
    db = next(get_mongo_db())
    redis_client = await get_async_redis()
    job_crud = JobStatusCRUD(db)

    settings = SettingsProvider.sget()

    # Root-scoped public storage is used for listing, staging copies, and cleanup.
    storage = get_public_storage(object_prefix="")

    try:
        all_users_data: List[Dict[str, str]] = []

        if settings.INGESTION_WORKER_S3_SYNC_MODE == "WHITELIST":
            logger.info(
                f"Running in WHITELIST mode for {len(settings.INGESTION_WORKER_S3_SYNC_WHITELIST_USER_IDS)} specified user(s)."
            )
            # Keep 'id' key here, as this data structure does not come from the DB
            # and UserORM.model_validate expects the 'id' field.
            all_users_data = [
                {"id": user_id} for user_id in settings.INGESTION_WORKER_S3_SYNC_WHITELIST_USER_IDS
            ]

        elif settings.INGESTION_WORKER_S3_SYNC_MODE == "ALL_USERS":
            # TODO (Scalability): For 100k+ users, query a dedicated 'FileSyncConfig'
            # collection instead of fetching all users.
            blacklist_ids = settings.INGESTION_WORKER_S3_SYNC_BLACKLIST_USER_IDS or []
            logger.info(
                f"Running in ALL_USERS mode. Excluding {len(blacklist_ids)} blacklisted user(s) at the database level."
            )
            # Efficiently exclude blacklisted users directly in the database query.
            # The primary key in the 'Users' collection is conclusively '_id'.
            query = {}
            if blacklist_ids:
                query = {"_id": {"$nin": blacklist_ids}}
            cursor = db["Users"].find(query)
            all_users_data = await cursor.to_list(length=None)
        else:
            logger.error(
                f"Invalid SYNC_MODE configured: '{settings.INGESTION_WORKER_S3_SYNC_MODE}'. Aborting nightly sync job."
            )
            return

        # --- DEFENSE-IN-DEPTH: Apply blacklist universally as a final safety filter ---
        # This ensures the blacklist always wins, even if a user is on the whitelist.
        blacklisted_set = set(settings.INGESTION_WORKER_S3_SYNC_BLACKLIST_USER_IDS or [])

        # This robust helper correctly gets the user ID from either 'id' (whitelist dict) or '_id' (DB dict).
        def _get_user_id(user_dict: Dict) -> str:
            return str(user_dict.get("id") or user_dict.get("_id") or "")

        users_to_process = [u for u in all_users_data if _get_user_id(u) not in blacklisted_set]

        logger.info(f"Identified {len(users_to_process)} user(s) to process for file sync.")

        for user_data in users_to_process:
            user = UserORM.model_validate(user_data)
            user_id = str(user.id)

            user_source_prefix = f"files/{user_id}/"
            staging_area_prefix = "staging/"

            logger.bind(ctx=ctx).info(
                f"Scanning object-storage prefix: "
                f"{storage.provider}://{storage.container_name}/{user_source_prefix}"
            )

            try:
                processed_files_collection = db["processed_files_log"]
                s3_file_metas = await storage.list(prefix=user_source_prefix)
                for s3_file in s3_file_metas:
                    original_source_key = s3_file.key

                    log_entry = await processed_files_collection.find_one(
                        {"_id": original_source_key, "user_id": user_id}
                    )
                    if log_entry and log_entry.get("status") in ["success", "queued"]:
                        continue

                    logger.bind(ctx=ctx).info(
                        f"Found new file '{original_source_key}', staging for ingestion."
                    )

                    job_id = str(uuid.uuid4())
                    original_filename = await storage.get_filename(original_source_key)
                    staging_key = f"{staging_area_prefix}{job_id}/{original_filename}"

                    # Calculate permanent URL for UI and Worker consistency
                    permanent_url = await storage.get_https_url(original_source_key)

                    await storage.copy(
                        source_key=original_source_key,
                        destination_key=staging_key,
                    )
                    logger.bind(ctx=ctx).info(
                        f"File copied to staging area: "
                        f"{storage.provider}://{storage.container_name}/{staging_key}"
                    )

                    job_payload = {
                        "job_id": job_id,
                        "user_id": user_id,
                        "tenant_id": user_id,
                        "kb_id": None,
                        "collection_name": "synced_files",
                        "original_filename": original_filename,
                        "s3_key": staging_key,
                        "original_source_key": original_source_key,
                        "persisted_s3_key": original_source_key,
                        "persisted_s3_url": str(permanent_url),
                        "job_type": "low_priority",
                        "targets": ["weaviate", "graphrag"],
                    }

                    await job_crud.create(
                        JobStatusCreate(
                            _id=job_id,
                            user_id=user_id,
                            tenant_id=user_id,
                            kb_id=None,
                            collection_name="synced_files",
                            original_filename=original_filename,
                            targets=["weaviate", "graphrag"],
                            s3_url=str(permanent_url),
                            uploaded_by_user_id=user_id,
                            # uploaded_by_email is omitted for perf; UI/API handles fallback
                        )
                    )

                    try:
                        await redis_client.lpush(
                            settings.REDIS_INGESTION_LOW_PRIORITY_QUEUE,
                            json.dumps(job_payload),
                        )

                        # Only update log on successful enqueue
                        await processed_files_collection.update_one(
                            {"_id": original_source_key, "user_id": user_id},
                            {"$set": {"status": "queued", "queued_at": datetime.datetime.utcnow()}},
                            upsert=True,
                        )
                    except Exception as e:
                        logger.bind(ctx=ctx).error(f"Failed to enqueue job {job_id}: {e}")
                        # Mark job as failed so it doesn't look stuck in UI
                        await job_crud.update_by_id(
                            job_id,
                            JobStatusUpdate(
                                status=JobState.FAILED,
                                weaviate_status=BackendState.FAILED,
                                graphrag_status=BackendState.FAILED,
                                error_message=f"Failed to enqueue nightly sync job: {str(e)}",
                            ),
                            user_id,
                        )
                        # Best-effort cleanup of staging object
                        try:
                            await storage.delete_key(staging_key)
                        except Exception as cleanup_e:
                            logger.bind(ctx=ctx).warning(
                                f"Failed to cleanup staging key {staging_key}: {cleanup_e}"
                            )

            except Exception as user_loop_exc:
                logger.bind(ctx=ctx).error(f"Error processing user {user_id}: {user_loop_exc}")

    except Exception as job_exc:
        logger.bind(ctx=ctx).exception(f"Nightly S3 sync failed: {job_exc}")
    finally:
        logger.bind(ctx=ctx).info("Finished nightly S3 sync job.")
