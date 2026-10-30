import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorClient
from redis_fastapi import redis_lifespan

from src.autobots.action.action_type.action_text2text.action_data_plane.data_plane_v2_crud import (
    ensure_data_plane_indexes,
)
from src.autobots.action.action_type.tool_routes import register_tools
from src.autobots.conn.temporal.worker import create_worker
from src.autobots.core.database.mongo_base import close_mongo_client, close_pymongo_client
from src.autobots.core.heartbeat.running_executions import HeartbeatPublisher
from src.autobots.core.logging.loguru_handler import replace_logging_with_loguru
from src.autobots.core.logging.setup_logger import setup_logger
from src.autobots.core.redis.async_pool import set_app
from src.autobots.core.settings import SettingsProvider
from src.autobots.integrations.crypto.encoder_decoder import init_crypto_keys


@asynccontextmanager
async def lifespan_runner(app: FastAPI):

    worker = None
    worker_task = None
    heartbeat = HeartbeatPublisher()

    async with redis_lifespan(app):
        set_app(app)

        logger.info("Starting autobots-runner")
        setup_logger()
        replace_logging_with_loguru()
        register_tools()

        await heartbeat.start()

        settings = SettingsProvider.sget()
        if settings.ENCRYPTION_PUBLIC_KEY_B64 and settings.ENCRYPTION_PRIVATE_KEY_B64:
            init_crypto_keys(settings.ENCRYPTION_PUBLIC_KEY_B64, settings.ENCRYPTION_PRIVATE_KEY_B64)
            logger.info("App creds crypto initialized")
        else:
            logger.warning("App creds crypto keys not set — app connections will fail")

        temp_client = AsyncIOMotorClient(settings.MONGO_CONN)
        try:
            await ensure_data_plane_indexes(temp_client[settings.MONGO_DATABASE])
        except Exception as e:
            logger.error(f"data_plane_v2 index setup failed (non-fatal): {e}")
        finally:
            temp_client.close()


        worker = await create_worker()
        worker_task = asyncio.create_task(worker.run())
        app.state.worker_task = worker_task
        logger.info(f"autobots-runner is polling task queue '{settings.TEMPORAL_TASK_QUEUE}'")

        try:
            yield
        finally:
            logger.info("Stopping autobots-runner")
            await worker.shutdown()
            if not worker_task.done():
                worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)

            await heartbeat.stop()

            close_mongo_client()
            close_pymongo_client()
            logger.info("autobots-runner stopped")
