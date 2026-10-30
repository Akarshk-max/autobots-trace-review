import base64
import html
import json
from typing import Optional, cast

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from loguru import logger
from pydantic import BaseModel, EmailStr, HttpUrl
from starlette import status
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from supabase_auth import AuthResponse, UserAttributes

from src.autobots.auth.auth import OAuthProvider, OAuthUrlRes, get_auth
from src.autobots.auth.data_models import BearerAccessToken, JwtPayload
from src.autobots.auth.security import (
    get_offline_user_from_access_token,
    get_offline_verified_access_token_str,
    get_online_user_from_creds,
    get_online_verified_access_token_str,
    try_get_jwt_payload_from_access_token,
)
from src.autobots.core.settings import SettingsProvider

router = APIRouter(prefix=SettingsProvider.sget().API_AUTH, tags=[SettingsProvider.sget().API_AUTH])


@router.post("/")
async def return_user_and_session(form_data: OAuth2PasswordRequestForm = Depends()) -> AuthResponse:
    """
    Returns Auth Response (User and Session)
    :return: AuthResponse
    """
    user_email_str = cast(EmailStr, form_data.username)
    auth_res: AuthResponse = await get_auth().sign_in_with_password(
        user_email_str, form_data.password
    )

    return auth_res


class OAuthExchangeReq(BaseModel):
    code: str
    verifier: str


class SignUpReq(BaseModel):
    email: EmailStr
    password: str
    redirect_to: Optional[HttpUrl] = None
    # user_metadata, mirrors supabase-js signUp options.data (e.g. name / full_name)
    data: Optional[dict] = None


class VerifyOtpReq(BaseModel):
    type: str
    token_hash: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    token: Optional[str] = None


class ResendReq(BaseModel):
    type: str
    email: EmailStr
    redirect_to: Optional[HttpUrl] = None


class SignOutRes(BaseModel):
    success: bool


@router.post("/signup")
async def sign_up(body: SignUpReq) -> AuthResponse:
    """
    Sign up a new user. Mirrors ``supabase.auth.signUp`` and returns the same
    User + Session shape (``session`` is null when email confirmation is required;
    ``user.identities`` is empty when the email already exists).
    """
    return await get_auth().sign_up_with_password(
        body.email,
        body.password,
        str(body.redirect_to) if body.redirect_to else None,
        body.data,
    )


@router.get("/user")
async def get_user(
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
) -> supabase_auth.User:
    """Return the currently authenticated user. Mirrors ``supabase.auth.getUser``."""
    return user_res.user


@router.post("/signout")
async def sign_out(access_token: str = Depends(get_offline_verified_access_token_str)) -> SignOutRes:
    """Revoke the caller's session at GoTrue. Mirrors ``supabase.auth.signOut``."""
    await get_auth().sign_out_with_token(access_token)
    return SignOutRes(success=True)


@router.post("/verify-otp")
async def verify_otp(body: VerifyOtpReq) -> AuthResponse:
    """Verify an OTP / email confirmation token. Mirrors ``supabase.auth.verifyOtp``."""
    params = body.model_dump(exclude_none=True)
    return await get_auth().verify_otp(params)


@router.post("/resend")
async def resend(body: ResendReq) -> supabase_auth.AuthOtpResponse:
    """Resend a signup / email-change OTP. Mirrors ``supabase.auth.resend``."""
    credentials: dict = {"type": body.type, "email": body.email}
    if body.redirect_to:
        credentials["options"] = {"email_redirect_to": str(body.redirect_to)}
    return await get_auth().resend(credentials)


@router.patch("/user")
async def update_user_attributes(
    attributes: UserAttributes,
    access_token: str = Depends(get_online_verified_access_token_str),
) -> supabase_auth.User:
    """
    Update the caller's own user (email/password/metadata) and return the updated
    User. Mirrors ``supabase.auth.updateUser``. (The legacy ``PUT /auth/user``
    keeps its browser-oriented redirect behaviour for existing consumers.)
    """
    user_res = await get_auth().update_user_with_token(access_token, dict(attributes))
    return user_res.user


@router.get("/oauth/url")
async def oauth_url(provider: OAuthProvider, redirect_to: HttpUrl) -> OAuthUrlRes:
    """
    Start a backend-mediated social login (PKCE). Returns the provider authorize
    URL to open and the PKCE ``verifier`` the caller holds and returns on exchange.
    We do NOT send our own ``state`` — it's reserved and managed by GoTrue; the code
    is bound to this caller by the ``verifier``, which never enters the browser.
    Stateless — no store.
    """
    return await get_auth().oauth_authorize_url(provider, str(redirect_to))


@router.post("/oauth/exchange")
async def oauth_exchange(body: OAuthExchangeReq) -> AuthResponse:
    """
    Complete a social login: exchange the authorization ``code`` + the PKCE
    ``verifier`` from :func:`oauth_url` for a User + Session.
    """
    return await get_auth().exchange_oauth_code(body.code, body.verifier)


def _cli_paste_token(code: str) -> str:
    """Wrap the authorization ``code`` in one opaque copy-paste token for the terminal.

    The user copies this single string; the CLI base64url-decodes it and exchanges the
    ``code`` with its held verifier. The PKCE ``verifier`` is NEVER included here — it
    must not travel through the browser, and it is the sole secret binding the code to
    this CLI (GoTrue owns ``state``, so we don't round-trip our own).
    """
    payload = json.dumps({"code": code}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()


def _cli_callback_page(*, code: str | None, error: str | None) -> str:
    """Render the terminal copy-paste landing page for headless CLI OAuth.

    GoTrue redirects the browser here with ``?code=...`` after a successful handshake.
    We show a single opaque token wrapping the ``code`` for the user to paste back into
    their terminal; the CLI decodes it and calls ``POST /auth/oauth/exchange`` with its
    held verifier. Nothing here is Supabase-specific — the CLI never sees Supabase.
    """
    if error:
        body = f"""
        <h1>Sign in failed</h1>
        <p class="muted">{html.escape(error)}</p>
        <p class="muted">Close this window and try again in your terminal.</p>
        """
    elif code:
        # base64url token — only [A-Za-z0-9_-], so inherently HTML-safe; escaped anyway.
        safe = html.escape(_cli_paste_token(code))
        body = f"""
        <h1>You're signed in</h1>
        <p class="muted">Copy this code and paste it back into your terminal.</p>
        <div class="code" id="code">{safe}</div>
        <button id="copy" onclick="navigator.clipboard.writeText(document.getElementById('code').textContent).then(()=>{{document.getElementById('copy').textContent='Copied'}})">Copy code</button>
        <p class="muted small">You can close this window after copying.</p>
        """
    else:
        body = """
        <h1>Nothing to show</h1>
        <p class="muted">This page is opened automatically at the end of a terminal sign in.</p>
        """
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terminal sign in</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         display:flex; min-height:100vh; align-items:center; justify-content:center; margin:0; }}
  .card {{ max-width:420px; padding:40px; text-align:center; }}
  h1 {{ font-size:20px; margin:0 0 8px; }}
  .muted {{ color:#888; font-size:14px; line-height:1.5; }}
  .small {{ font-size:12px; margin-top:24px; }}
  .code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:13px;
          word-break:break-all; background:rgba(127,127,127,.12); border-radius:8px;
          padding:14px; margin:20px 0; user-select:all; }}
  button {{ font-size:14px; padding:10px 20px; border-radius:8px; border:1px solid #8884;
           background:transparent; color:inherit; cursor:pointer; }}
  button:hover {{ background:rgba(127,127,127,.12); }}
</style></head>
<body><div class="card">{body}</div></body></html>"""


@router.get("/oauth/cli-callback", response_class=HTMLResponse)
async def oauth_cli_callback(
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    """
    Browser landing page for the headless/paste-back CLI login flow.

    Used as the ``redirect_to`` when a loopback server isn't available (SSH,
    remote box). GoTrue redirects here with ``?code=...``; we render a single opaque
    token wrapping that code for the user to copy back into their terminal, and the CLI
    exchanges it with its held verifier. (GoTrue also appends its own ``state`` — its
    reserved CSRF token — which we ignore; PKCE is what binds the code to the CLI.)
    """
    return HTMLResponse(
        _cli_callback_page(code=code, error=error or error_description),
        # The page shows a single-use authorization code — never cache it.
        headers={"Cache-Control": "no-store"},
    )


@router.post(SettingsProvider.sget().API_AUTH_TOKEN)
async def return_token(form_data: OAuth2PasswordRequestForm = Depends()) -> BearerAccessToken:
    """
    OAuth2 endpoint
    signs in user(username/password) and returns JWT token
    :return: BearerToken
    """
    user_email_str = cast(EmailStr, form_data.username)
    auth_res: AuthResponse = await get_auth().sign_in_with_password(
        user_email_str, form_data.password
    )

    bearer_token: BearerAccessToken = BearerAccessToken(
        access_token=auth_res.session.access_token, token_type="Bearer"
    )

    return bearer_token


@router.post("/token/test")
async def test_auth_access_token(
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
) -> supabase_auth.User:
    return user_res.user


@router.post("/creds/test")
async def test_auth_creds(
    user_res: supabase_auth.UserResponse = Depends(get_online_user_from_creds),
) -> supabase_auth.User:
    return user_res.user


@router.post("/password/reset")
async def reset_password_email(email: EmailStr, redirect_to: Optional[HttpUrl] = None) -> bool:
    return await get_auth().reset_password_email(email, redirect_to)


@router.post("/session/refresh")
async def refresh_password_email(
    refresh_token: str,
    _jwt_payload: JwtPayload | None = Depends(try_get_jwt_payload_from_access_token),
) -> AuthResponse:
    return await get_auth().refresh_session(refresh_token)


@router.put("/user")  # update_user
async def update_user(
    request: Request,
    attributes: UserAttributes,
    access_token: str = Depends(get_online_verified_access_token_str),
):
    # Forward the caller's own token to GoTrue (stateless / multi-worker safe) instead of
    # the shared server-side client's session, which holds no per-user auth. Same route,
    # method, body and redirect/cookie behaviour as before — only the mechanism changed.
    updated_user = await get_auth().update_user_with_token(access_token, dict(attributes))

    response = RedirectResponse(url="/user", status_code=status.HTTP_303_SEE_OTHER)

    if (
        updated_user
        and updated_user.user
        and updated_user.user.email
        and attributes.__contains__("password")
    ):
        user_email_str = cast(EmailStr, updated_user.user.email)
        auth_res: AuthResponse = await get_auth().sign_in_with_password(
            user_email_str, attributes.get("password")
        )

        response.set_cookie(
            "Authorization",
            value=f"Bearer {auth_res.session.access_token}",
            domain=request.base_url.hostname,
            httponly=True,
            max_age=1800,
            expires=1800,
        )

    return response


@router.get("/confirm")
async def confirm_user(
    request: Request,
    token_hash: str,
    type: str,
    next: str = None,
):
    ctx = request.state.context
    try:
        auth_res = await get_auth().verify_otp({"token_hash": token_hash, "type": type})

        if not next:
            bearer_token: BearerAccessToken = BearerAccessToken(
                access_token=auth_res.session.access_token, token_type="Bearer"
            )
            logger.bind(ctx=ctx).info("Confirmed user and created token")

            return bearer_token
        else:
            response = RedirectResponse(
                url=next,
                headers={"Authorization": f"Bearer {auth_res.session.access_token}"},
                status_code=status.HTTP_303_SEE_OTHER,
            )
            if SettingsProvider.sget().COOKIE_DOMAIN.find(request.base_url.hostname) >= 0:
                response.set_cookie(
                    "Authorization",
                    value=f"Bearer {auth_res.session.access_token}",
                    domain=request.base_url.hostname,
                    httponly=True,
                    max_age=1800,
                    expires=1800,
                )
            return response

    except Exception as e:
        logger.bind(ctx=ctx).error(f"Error in confirm user: {str(e)}")
        raise HTTPException(status_code=401, detail=str(e))
