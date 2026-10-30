from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from src.autobots.action.action_type.tool_routes import register_tools
from src.autobots.browser_task.runtime.playwright_service import playwright_scrape_service
from src.autobots.core.database.mongo_base import close_mongo_client, close_pymongo_client
from src.autobots.core.logging.loguru_handler import replace_logging_with_loguru
from src.autobots.core.logging.setup_logger import setup_logger


@asynccontextmanager
async def lifespan_playwright(app: FastAPI):
    setup_logger()
    replace_logging_with_loguru()
    register_tools()

    # start the service once at boot
    await playwright_scrape_service.start()

    logger.info("Lifespan: Playwright Server started")
    try:
        yield
    finally:
        logger.info("Stopping Playwright Container server")

        # stop service cleanly
        await playwright_scrape_service.stop()

        close_mongo_client()
        close_pymongo_client()
        logger.info("Playwright Container Server stopped")
