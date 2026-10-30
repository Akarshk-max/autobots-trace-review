from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorClient
from redis_fastapi import redis_lifespan

from src.autobots.core.database.mongo_base import (
    close_mongo_client,
    close_pymongo_client,
)
from src.autobots.core.database.mongo_indexes import ensure_indexes
from src.autobots.core.logging.loguru_handler import replace_logging_with_loguru
from src.autobots.core.logging.setup_logger import setup_logger
from src.autobots.core.redis.async_pool import set_app
from src.autobots.core.settings import SettingsProvider
from src.autobots.integrations.crypto.encoder_decoder import init_crypto_keys


@asynccontextmanager
async def lifespan_dashboard(app: FastAPI):
    async with redis_lifespan(app):
        set_app(app)

        logger.info("Starting dashboard server")
        setup_logger()
        replace_logging_with_loguru()

        settings = SettingsProvider.sget()
        if settings.ENCRYPTION_PUBLIC_KEY_B64 and settings.ENCRYPTION_PRIVATE_KEY_B64:
            init_crypto_keys(settings.ENCRYPTION_PUBLIC_KEY_B64, settings.ENCRYPTION_PRIVATE_KEY_B64)
            logger.info("App creds crypto initialized")
        else:
            logger.warning("App creds crypto will not be initialized and It will cause error while doing app connection")

        temp_client = AsyncIOMotorClient(settings.MONGO_CONN)
        try:
            await ensure_indexes(temp_client[settings.MONGO_DATABASE])
        except Exception as e:
            logger.error(f"CRUD index setup failed (non-fatal): {e}")
        finally:
            temp_client.close()

        logger.info("Lifespan: Dashboard server started")

        try:
            yield
        finally:
            logger.info("Stopping dashboard server")
            close_mongo_client()
            close_pymongo_client()
            logger.info("dashboard server stopped")
