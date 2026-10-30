from fastapi import APIRouter

from src.autobots.api.v1.api_hello import api_hello
from src.autobots.api.v1_dashboard import v1_dashboard_api_docs
from src.autobots.api.v1_dashboard.api_run_metrics import api_run_metrics
from src.autobots.core.settings import SettingsProvider

# API v1 router
dashboard_router = APIRouter(prefix=SettingsProvider.sget().API_v1)

dashboard_router.include_router(api_hello.router)
dashboard_router.include_router(api_run_metrics.router)

# api docs router
# router_docs = APIRouter(prefix=SettingsProvider.sget().API_v1)
dashboard_router.include_router(v1_dashboard_api_docs.router, prefix="", tags=["docs"])
