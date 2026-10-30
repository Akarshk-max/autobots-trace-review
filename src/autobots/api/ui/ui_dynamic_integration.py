from fastapi import APIRouter, Depends, Request
from supabase_auth import UserResponse

from src.autobots import SettingsProvider
from src.autobots.api.deps import get_admin_user_from_cookie
from src.autobots.api.ui import templates

router = APIRouter()


@router.get("/dynamic_integration")
async def page_dynamic_integration(
    request: Request, user: UserResponse | None = Depends(get_admin_user_from_cookie)
):
    if not user:
        return templates.TemplateResponse(request, "index.html")

    settings = SettingsProvider.sget()
    if user:
        return templates.TemplateResponse(
            request,
            "dynamic_integration.html",
            {"user": user.user, "version": settings.VERSION},
        )


@router.get("/dynamic_integration/quote")
async def dynamic_integration_title(
    request: Request, user: UserResponse | None = Depends(get_admin_user_from_cookie)
):
    if not user:
        return "Not Authorized"

    return "Dynamic Integration"
