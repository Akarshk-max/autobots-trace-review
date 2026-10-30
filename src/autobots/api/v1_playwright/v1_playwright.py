from fastapi import APIRouter

from src.autobots.api.v1.api_hello import api_hello
from src.autobots.api.v1_playwright import v1_playwright_api_docs
from src.autobots.api.v1_playwright.api_playwright import api_playwright
from src.autobots.core.settings import SettingsProvider

# API v1 router
playwight_router = APIRouter(prefix=SettingsProvider.sget().API_v1)

playwight_router.include_router(api_hello.router)
playwight_router.include_router(api_playwright.router)

# api docs router
# router_docs = APIRouter(prefix=SettingsProvider.sget().API_v1)
playwight_router.include_router(v1_playwright_api_docs.router, prefix="", tags=["docs"])
