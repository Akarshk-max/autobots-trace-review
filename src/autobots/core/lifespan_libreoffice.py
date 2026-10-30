from contextlib import asynccontextmanager

# import nest_asyncio
from fastapi import FastAPI
from loguru import logger

from src.autobots.action.action_type.tool_routes import register_tools
from src.autobots.core.database.mongo_base import close_mongo_client, close_pymongo_client
from src.autobots.core.logging.loguru_handler import replace_logging_with_loguru
from src.autobots.core.logging.setup_logger import setup_logger


@asynccontextmanager
async def lifespan_libreoffice(app: FastAPI):
    logger.info("Starting libreoffice container server")
    setup_logger()
    replace_logging_with_loguru()
    register_tools()
    # nest_asyncio.apply()
    logger.info("Lifespan: Libreoffice Server started")

    yield

    logger.info("Stopping Libreoffice Container server")
    logger.info("Clean up and release the resources")
    close_mongo_client()
    close_pymongo_client()
    logger.info("Libreoffice Container Server stopped")
