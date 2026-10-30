import asyncio
from contextlib import asynccontextmanager

from src.autobots.data_model.app_state import AppState
from fastapi import FastAPI
from loguru import logger
from redis_fastapi import redis_lifespan

from src.autobots.action.action_type.tool_routes import register_tools
from src.autobots.integrations.crypto.encoder_decoder import init_crypto_keys
from src.autobots.core.database.mongo_base import (
    close_mongo_client,
    close_pymongo_client,
    get_mongo_db,
)
from src.autobots.core.logging.loguru_handler import replace_logging_with_loguru
from src.autobots.core.logging.setup_logger import setup_logger
from src.autobots.core.redis.async_pool import set_app
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_monitor.event_monitor import EventMonitor
from src.autobots.schedule.schedule import Schedule


@asynccontextmanager
async def lifespan_scheduler(app: FastAPI):
    async with redis_lifespan(app):
        set_app(app)

        logger.info("Starting scheduler server")
        setup_logger()
        replace_logging_with_loguru()
        register_tools()

        settings = SettingsProvider.sget()
        if settings.ENCRYPTION_PUBLIC_KEY_B64 and settings.ENCRYPTION_PRIVATE_KEY_B64:
            init_crypto_keys(settings.ENCRYPTION_PUBLIC_KEY_B64, settings.ENCRYPTION_PRIVATE_KEY_B64)
            logger.info("App creds crypto initialized")
        else:
            logger.warning("App creds crypto will not be initialized and It will cause error while doing app connection")

        asyncio.create_task(EventMonitor.run_from_settings(ctx=Context(), app_state=AppState(), db=next(get_mongo_db())))
        logger.info("Event monitor started")
        Schedule.start_scheduler()
        logger.info("Lifespan: Scheduler server started")

        yield

        logger.info("Stopping scheduler server")
        logger.info("Clean up and release the resources")
        Schedule.stop_scheduler()
        close_mongo_client()
        close_pymongo_client()
        logger.info("Scheduler server stopped")
