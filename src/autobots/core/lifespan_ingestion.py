import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorClient
from redis_fastapi import redis_lifespan

from src.autobots.integrations.crypto.encoder_decoder import init_crypto_keys
from src.autobots.core.database.mongo_base import (
    close_mongo_client,
    close_pymongo_client,
    get_mongo_db,
)
from src.autobots.core.logging.setup_logger import setup_logger
from src.autobots.core.redis.async_pool import set_app
from src.autobots.core.settings import SettingsProvider
from src.autobots.ingestion.neo4j_loader import Neo4jManager
from src.autobots.ingestion.realtime_indexer import RealtimeIndexer
from src.autobots.ingestion.worker import main_worker_loop
from src.autobots.schedule.schedule import Schedule, run_nightly_s3_sync


@asynccontextmanager
async def lifespan_ingestion(app: FastAPI):
    """Lifespan for ingestion-worker service.

    Important: redis_fastapi's pool is initialized/teardown via `redis_lifespan(app)`.
    The entire worker lifetime (startup → yield → shutdown) must run inside that
    context manager.
    """

    worker_task = None
    indexer_task = None
    indexer: RealtimeIndexer | None = None

    async with redis_lifespan(app):
        set_app(app)

        logger.info("Starting up Ingestion/RAG Service...")
        setup_logger()

        settings = SettingsProvider.sget()

        if settings.ENCRYPTION_PUBLIC_KEY_B64 and settings.ENCRYPTION_PRIVATE_KEY_B64:
            init_crypto_keys(settings.ENCRYPTION_PUBLIC_KEY_B64, settings.ENCRYPTION_PRIVATE_KEY_B64)
            logger.info("App creds crypto initialized")
        else:
            logger.warning("App creds crypto will not be initialized and It will cause error while doing app connection")


        # --- One-time setup: Ensure performance indexes exist ---
        temp_mongo_client = None
        try:
            # Create a new, temporary client just for this startup task.
            temp_mongo_client = AsyncIOMotorClient(settings.MONGO_CONN)
            db = temp_mongo_client[settings.MONGO_DATABASE]
            logger.info("Ensuring all required MongoDB indexes exist for worker and indexer...")

            # --- Indexes for the File Status API ---
            # Index for default sorting by creation date.
            await db["IngestionJobs"].create_index(
                [("user_id", 1), ("created_at", -1)],
                name="user_files_list_idx",
                background=True,
            )

            # Compound indexes to support efficient sorting on different columns.
            # These cover the `sort_by` options in the API.
            await db["IngestionJobs"].create_index(
                [("user_id", 1), ("updated_at", -1)],
                name="user_files_updated_at_idx",
                background=True,
            )
            # This index supports both filtering by status and sorting by status.
            # The created_at field is added to make the sort order deterministic.
            await db["IngestionJobs"].create_index(
                [("user_id", 1), ("status", 1), ("created_at", -1)],
                name="user_files_status_sort_idx",
                background=True,
            )
            await db["IngestionJobs"].create_index(
                [("user_id", 1), ("original_filename", 1)],
                name="user_files_filename_sort_idx",
                background=True,
            )

            # --- ADDED: Indexes for "KB Inventory" (Tenant-Scoped Views) ---
            # These are required because Shared KB views query by 'tenant_id', not 'user_id'.

            # 1. Default sort (newest first)
            await db["IngestionJobs"].create_index(
                [("tenant_id", 1), ("created_at", -1)],
                name="tenant_files_list_idx",
                background=True,
            )

            # 2. Sort by updated time
            await db["IngestionJobs"].create_index(
                [("tenant_id", 1), ("updated_at", -1)],
                name="tenant_files_updated_at_idx",
                background=True,
            )

            # 3. Filter by status (e.g. "Show me failed jobs in this KB")
            await db["IngestionJobs"].create_index(
                [("tenant_id", 1), ("status", 1), ("created_at", -1)],
                name="tenant_files_status_sort_idx",
                background=True,
            )

            # 4. Search/Sort by filename within a KB
            await db["IngestionJobs"].create_index(
                [("tenant_id", 1), ("original_filename", 1)],
                name="tenant_files_filename_sort_idx",
                background=True,
            )

            # 5. Indexes for "My Uploads" (Filter by Uploader across all KBs)
            # These mirror the user_id and tenant_id indexes to support full UI sorting/filtering
            # when viewing files via the `uploaded_by_user_id` field.

            # Default sort (created_at desc)
            await db["IngestionJobs"].create_index(
                [("uploaded_by_user_id", 1), ("created_at", -1)],
                name="uploader_files_list_idx",
                background=True,
            )
            # Sort by updated_at
            await db["IngestionJobs"].create_index(
                [("uploaded_by_user_id", 1), ("updated_at", -1)],
                name="uploader_files_updated_at_idx",
                background=True,
            )
            # Filter/Sort by Status
            await db["IngestionJobs"].create_index(
                [("uploaded_by_user_id", 1), ("status", 1), ("created_at", -1)],
                name="uploader_files_status_sort_idx",
                background=True,
            )
            # Sort/Search by Filename
            await db["IngestionJobs"].create_index(
                [("uploaded_by_user_id", 1), ("original_filename", 1)],
                name="uploader_files_filename_sort_idx",
                background=True,
            )

            # This index is critical for the performance of the real-time indexer's polling query.
            # Optimization: Only create indexes for collections that are actively being watched.
            for collection_name, targets in settings.INGESTION_WORKER_MONGO_INDEXER_TARGETS.items():
                if targets:  # Only if the target list is not empty
                    # --- CONDITIONAL INDEX CREATION ---
                    # Create the optimal index based on whether the collection uses a 'status' field.
                    if collection_name in {"EventResults"}:
                        # For EventResults, the partial index on 'status' is supported and highly effective.
                        await db[collection_name].create_index(
                            [("status", 1), ("indexed", 1), ("updated_at", 1), ("_id", 1)],
                            background=True,
                            partialFilterExpression={"status": "success"},
                        )
                    else:
                        # For other collections, we create a non-partial compound index. This is universally
                        # compatible and still highly performant as `indexed` will be very selective.
                        await db[collection_name].create_index(
                            [("indexed", 1), ("updated_at", 1), ("_id", 1)],
                            background=True,
                        )

            # Index for the state collection itself
            await db["RealtimeIndexingState"].create_index([("updated_at", 1)], background=True)
            logger.success("Successfully verified all required MongoDB indexes.")

            # --- Indexes for Knowledge Base RBAC ---
            try:
                # Enforce unique KB name per owner at the DB level to match app logic.
                await db["KnowledgeBases"].create_index(
                    [("owner_user_id", 1), ("name", 1)],
                    name="kb_owner_name_unique_idx",
                    unique=True,
                    background=True,
                )
                # Name-only index for efficient name-based lookups (e.g., resolve_kb_id_by_name_for_user).
                await db["KnowledgeBases"].create_index(
                    [("name", 1)],
                    name="kb_name_idx",
                    background=True,
                )
                await db["KnowledgeBases"].create_index(
                    [("tenant_id", 1)],
                    name="kb_tenant_id_unique_idx",
                    unique=True,
                    background=True,
                )
                await db["KnowledgeBaseMemberships"].create_index(
                    [("kb_id", 1), ("user_id", 1)],
                    name="kb_membership_kb_user_unique_idx",
                    unique=True,
                    background=True,
                )
                await db["KnowledgeBaseMemberships"].create_index(
                    [("user_id", 1)],
                    name="kb_membership_user_idx",
                    background=True,
                )
                await db["Users"].create_index(
                    [("email", 1)],
                    name="users_email_idx",
                    background=True,
                )
                # Index for robust user_id lookups in KB admin API
                await db["Users"].create_index(
                    [("user_id", 1)],
                    name="users_user_id_idx",
                    background=True,
                )
            except Exception as e:
                logger.error(f"Could not create KnowledgeBase/Users indexes during startup: {e}")

        except Exception as e:
            logger.error(f"Could not create required MongoDB indexes during startup: {e}")
            # This is not a fatal error, so we log it and continue startup.
        finally:
            # Ensure the temporary client is always closed, leaving the global one untouched.
            if temp_mongo_client:
                temp_mongo_client.close()
                logger.info("Closed temporary MongoDB client used for index creation.")

        # Start a scheduler instance that uses its own dedicated MongoDB collection for jobs.
        Schedule.start_scheduler(collection_name="ingestion_worker_jobs")

        # Start the main worker loop as a background task.
        logger.info("Starting background worker loop to consume Redis jobs...")
        worker_task = asyncio.create_task(main_worker_loop())

        # The indexer will use the main, globally-managed MongoDB client provided by get_mongo_db().
        # This client is properly closed on shutdown.
        db_for_indexer = next(get_mongo_db())
        indexer = RealtimeIndexer(db=db_for_indexer)
        indexer_task = asyncio.create_task(indexer.run_loop())
        logger.info("Background real-time indexer loop started.")

        ####################################################################################
        # Nightly S3 Sync Cron Job
        # ------------------------
        # PRODUCTION SETTING: This job is scheduled to run once per day at 2 AM UTC.
        #    trigger="cron",
        #    hour=2,
        #    minute=0,
        # FOR E2E TESTING:
        # To test this functionality, temporarily change the trigger below.
        # For example, to run every minute, change the trigger arguments to:
        #    trigger="cron",
        #    minute="*/1",
        ####################################################################################

        # Schedule the nightly S3 sync job. This job now belongs exclusively to this service.
        Schedule._scheduler.add_job(
            func=run_nightly_s3_sync,
            trigger="cron",
            # For production
            # hour=2,  # Run at 2 AM UTC
            hour="*",  # Run every hour
            minute=0,
            # For testing
            # minute="*/1",  # Run every minute for fast testing
            id="system_nightly_s3_sync",
            name="Nightly S3 User Folder Sync",
            replace_existing=True,
        )
        logger.info("Nightly S3 sync job scheduled by ingestion-worker.")

        yield

        logger.info("Shutting down Ingestion/RAG Service...")
        if indexer_task and not indexer_task.done():
            indexer_task.cancel()
        if worker_task and not worker_task.done():
            worker_task.cancel()

        if indexer is not None:
            await indexer.close_all_stores()

        Neo4jManager.shutdown()
        Schedule.stop_scheduler()

        # Correctly closes the global client used by the indexer and other components.
        close_mongo_client()
        close_pymongo_client()

        try:
            await asyncio.gather(*(t for t in [worker_task, indexer_task] if t is not None), return_exceptions=True)
            logger.info("All background tasks successfully shut down.")
        except asyncio.CancelledError:
            logger.info("Background tasks successfully cancelled.")
