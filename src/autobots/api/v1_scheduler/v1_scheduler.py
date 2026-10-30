from fastapi import APIRouter

from src.autobots.api.v1.api_hello import api_hello
from src.autobots.api.v1_scheduler import v1_scheduler_api_docs
from src.autobots.api.v1_scheduler.api_schedules import api_schedules
from src.autobots.core.settings import SettingsProvider

# API v1 router
scheduler_router = APIRouter(prefix=SettingsProvider.sget().API_v1)

scheduler_router.include_router(api_hello.router)
scheduler_router.include_router(api_schedules.router)

# api docs router
# router_docs = APIRouter(prefix=SettingsProvider.sget().API_v1)
scheduler_router.include_router(v1_scheduler_api_docs.router, prefix="", tags=["docs"])
