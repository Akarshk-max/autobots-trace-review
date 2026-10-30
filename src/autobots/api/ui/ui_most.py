import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Annotated, cast

import supabase_auth
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from loguru import logger
from pydantic import EmailStr
from starlette import status
from starlette.responses import RedirectResponse
from starlette.templating import Jinja2Templates
from supabase_auth import AuthResponse, UserResponse

from src.autobots.api.deps import get_admin_user_from_cookie
from src.autobots.api.v1.api_auth.api_auth import return_token
from src.autobots.auth.auth import get_auth
from src.autobots.auth.data_models import BearerAccessToken
from src.autobots.auth.security import (
    get_offline_user_from_access_token,
    get_offline_verified_access_token_str,
)
from src.autobots.core.settings import SettingsProvider

router = APIRouter()

templates = Jinja2Templates(directory="src/autobots/ui/templates")


@router.post("/cookie")
async def cookie(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    ctx = request.state.context
    settings = SettingsProvider.sget()
    try:
        email_str = cast(EmailStr, form_data.username)
        auth_res: AuthResponse = await get_auth().sign_in_with_password(
            email_str, form_data.password
        )
        user: supabase_auth.User | None = auth_res.user
        session: supabase_auth.Session | None = auth_res.session
        if not user or not session:
            raise HTTPException(status_code=401, detail="User Session not found")

        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        if settings.COOKIE_DOMAIN.find(request.base_url.hostname) >= 0:
            response.set_cookie(
                "Authorization",
                value=f"Bearer {auth_res.session.access_token}",
                domain=request.base_url.hostname,
                httponly=True,
                max_age=1800,
                expires=1800,
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Cookie domain codes not contain request base url: {request.base_url.hostname}",
            )
        return response
    except Exception as e:
        # Never log the password. Surface a friendly reason instead of silently
        # re-rendering a blank page, so failures are actually diagnosable.
        logger.bind(
            ctx=ctx,
            url=request.base_url.hostname,
            username=form_data.username,
        ).exception("Login error")
        # sign_in_with_password normalises bad creds to HTTPException(401); anything else
        # (e.g. cookie-domain misconfig, GoTrue outage) gets a generic message.
        is_bad_creds = isinstance(e, HTTPException) and e.status_code == 401
        message = "Invalid email or password." if is_bad_creds else "Unable to sign in. Please try again."
        return templates.TemplateResponse(
            request, "login.html", {"error": message}, status_code=401 if is_bad_creds else 400
        )


@router.get("/cookie/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/")
    # Actually revoke the session at GoTrue using the caller's own token (the shared
    # server-side client holds no per-user session, so get_auth().sign_out() is a no-op).
    # Best-effort — logout must never fail the redirect.
    cookie_val = request.cookies.get("Authorization")
    if cookie_val:
        token = cookie_val.removeprefix("Bearer ").strip()
        try:
            await get_auth().sign_out_with_token(token)
        except Exception as e:
            logger.bind(ctx=request.state.context).warning(f"Logout revoke failed: {e}")
    response.delete_cookie("Authorization", domain=request.base_url.hostname)
    return response


@router.post("/signup")
async def signup(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    ctx = request.state.context
    settings = SettingsProvider.sget()
    try:
        # Only allow users from the configured system email domain to register
        if not SettingsProvider.is_system_email(form_data.username):
            raise HTTPException(status_code=401, detail="User not in configured system email domain")

        user_email_str = cast(EmailStr, form_data.username)
        auth_res: AuthResponse = await get_auth().sign_up_with_password(
            user_email_str, form_data.password, str(request.base_url)
        )
        user: supabase_auth.User | None = auth_res.user
        session: supabase_auth.Session | None = auth_res.session
        if not user or not session:
            raise HTTPException(status_code=401, detail="User Session not found")

        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        if settings.COOKIE_DOMAIN.find(request.base_url.hostname) >= 0:
            response.set_cookie(
                "Authorization",
                value=f"Bearer {auth_res.session.access_token}",
                domain=request.base_url.hostname,
                httponly=True,
                max_age=1800,
                expires=1800,
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Cookie domain codes not contain request base url: {request.base_url.hostname}",
            )
        return response
    except Exception as e:
        # Never log the password. Surface the reason so failures aren't silent.
        logger.bind(
            ctx=ctx,
            url=request.base_url.hostname,
            username=form_data.username,
        ).exception("Sign up error")
        message = e.detail if isinstance(e, HTTPException) else "Unable to sign up. Please try again."
        return templates.TemplateResponse(
            request, "signup.html", {"error": message}, status_code=400
        )


@router.get("/")
async def page_index(
    request: Request, user: UserResponse | None = Depends(get_admin_user_from_cookie)
):
    settings = SettingsProvider.sget()
    if user:
        return templates.TemplateResponse(
            request, "home.html", {"user": user.user, "version": settings.VERSION}
        )
    else:
        return templates.TemplateResponse(request, "index.html")


@router.get("/signup")
async def page_signup(request: Request):
    return templates.TemplateResponse(request, "signup.html")


@router.get("/login")
async def page_login(request: Request):
    return templates.TemplateResponse(request, "login.html")


@router.get("/docs")
async def page_api_docs(
    request: Request, user: UserResponse | None = Depends(get_admin_user_from_cookie)
):
    settings = SettingsProvider.sget()
    if user:
        return templates.TemplateResponse(
            request, "docs.html", {"user": user, "version": settings.VERSION}
        )
    else:
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        return response


@router.get("/redoc")
async def page_api_redoc(
    request: Request, user: UserResponse | None = Depends(get_admin_user_from_cookie)
):
    settings = SettingsProvider.sget()
    if user:
        return templates.TemplateResponse(
            request, "redoc.html", {"user": user, "version": settings.VERSION}
        )
    else:
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        return response


@router.get("/logs")
async def page_logs(
    request: Request, user: UserResponse | None = Depends(get_admin_user_from_cookie)
):
    settings = SettingsProvider.sget()
    if user:
        return templates.TemplateResponse(
            request, "logs.html", {"user": user, "version": settings.VERSION}
        )
    else:
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        return response


@router.get("/user")
async def page_user(
    request: Request, user: UserResponse | None = Depends(get_admin_user_from_cookie)
):
    settings = SettingsProvider.sget()
    if user:
        return templates.TemplateResponse(
            request, "user.html", {"user": user, "version": settings.VERSION}
        )
    else:
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        return response


def _token_expiry(token: str) -> tuple[str | None, str | None]:
    """Best-effort ``(expires_at, expires_in)`` for display on the token page.

    Reads the ``exp`` claim straight off the JWT payload segment. No signature check:
    the token was just minted by our own auth service and this is display only — it
    tells a developer how long the token they are copying is good for, and nothing is
    authorised on the strength of it. Never fails the page.
    """
    try:
        segment = token.split(".")[1]
        padded = segment + "=" * (-len(segment) % 4)
        exp = json.loads(base64.urlsafe_b64decode(padded)).get("exp")
        if not exp:
            return None, None
        expires_at = datetime.fromtimestamp(int(exp), tz=UTC)
        minutes = int((expires_at - datetime.now(UTC)).total_seconds() // 60)
        if minutes <= 0:
            expires_in = None
        elif minutes < 60:
            expires_in = f"{minutes} min"
        else:
            expires_in = f"{minutes // 60} h {minutes % 60} min"
        return expires_at.strftime("%Y-%m-%d %H:%M UTC"), expires_in
    except (IndexError, ValueError, TypeError, binascii.Error, json.JSONDecodeError) as e:
        # Opaque or unexpected token shape — show the token, just without an expiry.
        logger.warning(f"Could not read access token expiry for display: {e}")
        return None, None


@router.get("/token")
async def page_token(
    request: Request, user: UserResponse | None = Depends(get_admin_user_from_cookie)
):
    settings = SettingsProvider.sget()
    if not user:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request, "token.html", {"user": user, "version": settings.VERSION}
    )


@router.post("/token")
async def process_token(
    request: Request,
    user: UserResponse | None = Depends(get_admin_user_from_cookie),
    form_data: OAuth2PasswordRequestForm = Depends(),
):
    """Mint an API access token for the signed-in user.

    Goes through the existing ``POST /auth/token`` handler (same credentials check,
    same BearerAccessToken shape) and renders the access token back on the page.
    """
    ctx = request.state.context
    settings = SettingsProvider.sget()
    if not user:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    try:
        bearer_token: BearerAccessToken = await return_token(form_data)
    except Exception as e:
        # Never log the password. Surface a friendly reason instead of a blank page.
        logger.bind(
            ctx=ctx,
            url=request.base_url.hostname,
            username=form_data.username,
        ).exception("Access token generation error")
        is_bad_creds = isinstance(e, HTTPException) and e.status_code == 401
        message = (
            "Invalid email or password."
            if is_bad_creds
            else "Unable to generate an access token. Please try again."
        )
        return templates.TemplateResponse(
            request,
            "token.html",
            {"user": user, "version": settings.VERSION, "error": message},
            status_code=401 if is_bad_creds else 400,
        )
    expires_at, expires_in = _token_expiry(bearer_token.access_token)
    return templates.TemplateResponse(
        request,
        "token.html",
        {
            "user": user,
            "version": settings.VERSION,
            "access_token": bearer_token.access_token,
            "token_type": bearer_token.token_type,
            "expires_at": expires_at,
            "expires_in": expires_in,
            "api_url": settings.AUTOBOTS_API_URL.rstrip("/"),
        },
        # The page carries a live credential — never cache it.
        headers={"Cache-Control": "no-store"},
    )


@router.get("/password/reset")
async def page_password_reset(
    request: Request, user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token)
):
    settings = SettingsProvider.sget()
    if user_res:
        return templates.TemplateResponse(
            request,
            "password_reset.html",
            {"user": user_res, "version": settings.VERSION},
        )
    else:
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        return response


@router.post("/password_reset")
async def process_password_reset(
    request: Request,
    new_password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
    access_token: str = Depends(get_offline_verified_access_token_str),
):
    ctx = request.state.context
    if new_password == confirm_password:
        # Update via the caller's own token (stateless / multi-worker safe).
        user = await get_auth().update_user_with_token(access_token, {"password": new_password})
        logger.bind(ctx=ctx).info(f"User password changed for {user.user.email}")
    else:
        logger.bind(ctx=ctx).info("User password NOT changed (passwords did not match)")
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return response
