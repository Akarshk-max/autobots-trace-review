import supabase_auth
from fastapi import APIRouter, Depends
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from starlette.responses import JSONResponse

from src.autobots.api.v1_libreoffice import v1_libreoffice
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.fastapi_desc_libreoffice import FastAPIDescLibreOffice
from src.autobots.core.settings import SettingsProvider

router = APIRouter()


@router.get("/openapi.json")
async def open_api_json(user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token)):
    fastapi_dec = FastAPIDescLibreOffice()
    return JSONResponse(
        get_openapi(
            title=fastapi_dec.title,
            version=fastapi_dec.version,
            description=fastapi_dec.description,
            routes=v1_libreoffice.libreoffice_router.routes,
            contact=fastapi_dec.contact,
            license_info=fastapi_dec.license_info,
        )
    )


@router.get("/docs")
async def api_docs(user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token)):
    settings = SettingsProvider.sget()
    return get_swagger_ui_html(openapi_url=f"{settings.API_v1}/openapi.json", title="docs")


@router.get("/redoc")
async def api_redoc(user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token)):
    settings = SettingsProvider.sget()
    return get_redoc_html(openapi_url=f"{settings.API_v1}/openapi.json", title="redoc")
