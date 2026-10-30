# src/autobots/api/v1_ingestion/v1_ingestion.py

from fastapi import APIRouter

from src.autobots.api.v1_ingestion.api_file_status import router as file_status_router
from src.autobots.api.v1_ingestion.api_jobs import router as jobs_router
from src.autobots.api.v1_ingestion.api_kb_admin import router as kb_admin_router
from src.autobots.api.v1_ingestion.api_knowledge_base import router as knowledge_base_router
from src.autobots.api.v1_ingestion.api_sandbox import router as sandbox_router
from src.autobots.core.settings import SettingsProvider

# Main router for v1_ingestion APIs
settings = SettingsProvider.sget()
router = APIRouter(prefix=settings.API_V1_INGESTION)

# Include sub-routers
router.include_router(file_status_router)
router.include_router(knowledge_base_router)
router.include_router(sandbox_router)
router.include_router(jobs_router)
router.include_router(kb_admin_router)
