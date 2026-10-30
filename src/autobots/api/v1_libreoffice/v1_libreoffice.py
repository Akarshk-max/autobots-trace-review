from fastapi import APIRouter

from src.autobots.api.v1.api_hello import api_hello
from src.autobots.api.v1_libreoffice import v1_libreoffice_api_docs
from src.autobots.api.v1_libreoffice.api_libres import api_libres
from src.autobots.core.settings import SettingsProvider

# API v1 router
libreoffice_router = APIRouter(prefix=SettingsProvider.sget().API_v1)

libreoffice_router.include_router(api_hello.router)
libreoffice_router.include_router(api_libres.router)

# api docs router
# router_docs = APIRouter(prefix=SettingsProvider.sget().API_v1)
libreoffice_router.include_router(v1_libreoffice_api_docs.router, prefix="", tags=["docs"])
