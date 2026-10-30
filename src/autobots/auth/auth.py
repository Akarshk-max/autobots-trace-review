import base64
import hashlib
import secrets
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
import supabase_auth
from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel, EmailStr, HttpUrl

from src.autobots.core.settings import SettingsProvider

# Bound every call out to GoTrue so a slow IdP can't tie up a worker indefinitely.
_GOTRUE_TIMEOUT = httpx.Timeout(10.0)
# Pin the GoTrue REST API version so response shapes match the supabase_auth models we
# parse into (this is the version the installed supabase-py client sends).
_GOTRUE_API_VERSION = "2024-01-01"

# This module talks to GoTrue over plain httpx rather than the supabase-py client.
# supabase-py is (a) synchronous — every call would block the event loop — and (b) built
# around a client that stores a per-user *session* internally. As a single shared,
# session-less singleton (one per worker) it can't safely serve per-caller operations
# (sign out / update user), and constructing a fresh client per request would still be
# synchronous and still need a session set on it. Forwarding the caller's own token (or,
# for unauthenticated calls, the anon key) straight to GoTrue is stateless, multi-worker
# safe, and natively async. Responses are parsed into the supabase_auth Pydantic models,
# so the returned shapes are identical to what supabase-py produced.

OAuthProvider = Literal["google", "azure"]


class OAuthUrlRes(BaseModel):
    url: str
    # PKCE code_verifier. The caller (the CLI — a public OAuth client) holds this and
    # sends it back on exchange, exactly as RFC-7636 intends. Stateless: nothing is
    # stored server-side, so the flow works across workers and login never depends on
    # a shared store. The verifier only travels the direct backend<->CLI TLS channel,
    # never the browser/provider, so it's never co-located with the authorization code.
    verifier: str


class Auth:
    """Stateless GoTrue REST client. Holds no per-user session (see module note)."""

    async def _gotrue(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: dict | None = None,
        json: Any | None = None,
    ) -> httpx.Response:
        """One GoTrue REST call. Returns the raw response; the caller checks status.

        ``token`` is the caller's bearer for session-scoped endpoints; unauthenticated
        endpoints fall back to the anon key, matching what supabase-py sends.
        """
        settings = SettingsProvider.sget()
        headers = {
            "apikey": settings.SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {token or settings.SUPABASE_ANON_KEY}",
            "Content-Type": "application/json",
            "X-Supabase-Api-Version": _GOTRUE_API_VERSION,
        }
        url = f"{settings.SUPABASE_URL}/auth/v1/{path}"
        kwargs: dict = {"headers": headers}
        if params is not None:
            kwargs["params"] = params
        if json is not None:
            kwargs["json"] = json
        async with httpx.AsyncClient(timeout=_GOTRUE_TIMEOUT) as client:
            res = await getattr(client, method.lower())(url, **kwargs)
        # Early warning if Supabase stops honouring our pinned API version: GoTrue echoes
        # the version it actually served. A mismatch means response shapes may have moved
        # and our supabase_auth model parsing could break — alert on this in Datadog.
        served = res.headers.get("x-supabase-api-version")
        if served and served != _GOTRUE_API_VERSION:
            logger.warning(
                f"GoTrue API version drift: requested {_GOTRUE_API_VERSION}, served "
                f"{served} (path=auth/v1/{path}). Check the supabase/auth changelog — "
                "response shapes may have changed."
            )
        return res

    @staticmethod
    def _gotrue_error(res: httpx.Response, *, status: int | None = None) -> HTTPException:
        """Map a GoTrue error response to an HTTPException without leaking its raw body."""
        try:
            body = res.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        code = body.get("error_code") or body.get("code")
        logger.warning(f"GoTrue error ({res.status_code}) code={code}: {res.text}")
        detail = body.get("msg") or body.get("error_description") or "Authentication failed"
        return HTTPException(status_code=status or res.status_code, detail=detail)

    @staticmethod
    def _to_auth_response(res: httpx.Response) -> supabase_auth.AuthResponse:
        """Parse a GoTrue body into AuthResponse.

        ``/token`` and a confirmed ``/verify`` return a full session (user nested); a
        signup awaiting email confirmation returns just the user (no ``access_token``).
        """
        data = res.json()
        if isinstance(data, dict) and data.get("access_token"):
            session = supabase_auth.Session(**data)
            return supabase_auth.AuthResponse(session=session, user=session.user)
        return supabase_auth.AuthResponse(session=None, user=supabase_auth.User(**data))

    async def oauth_authorize_url(self, provider: OAuthProvider, redirect_to: str) -> OAuthUrlRes:
        """
        Build a Supabase (GoTrue) PKCE authorize URL for a backend-mediated social
        login. Returns the URL to open and the ``verifier`` the caller holds and returns
        to :meth:`exchange_oauth_code`. Stateless — nothing is stored server-side.

        We do NOT pass our own ``state``: ``state`` is reserved and managed by GoTrue
        (it round-trips a signed JWT carrying the flow, incl. ``redirect_to``). Injecting
        a custom value overwrites that JWT and makes GoTrue reject its own callback with
        ``bad_oauth_state``. CSRF/binding is provided by PKCE — the authorization code is
        useless without the ``verifier``, which only the caller (CLI) holds and which
        never enters the browser.
        """
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )

        params = {
            "provider": provider,
            "redirect_to": redirect_to,
            "code_challenge": challenge,
            "code_challenge_method": "s256",
        }
        if provider == "google":
            # Ask Google for a refresh token so the session can be refreshed later.
            params |= {"access_type": "offline", "prompt": "consent"}
        else:  # azure / Outlook
            params["scopes"] = "email openid profile"

        base = SettingsProvider.sget().SUPABASE_URL
        return OAuthUrlRes(url=f"{base}/auth/v1/authorize?{urlencode(params)}", verifier=verifier)

    async def exchange_oauth_code(self, code: str, verifier: str) -> supabase_auth.AuthResponse:
        """
        Exchange an OAuth authorization ``code`` + PKCE ``verifier`` for a Supabase
        session. The caller holds the verifier from :meth:`oauth_authorize_url` and
        sends it back here, so nothing is stored server-side. Single-use is enforced
        by the provider, which burns the authorization code on first exchange.
        """
        res = await self._gotrue(
            "POST",
            "token",
            params={"grant_type": "pkce"},
            json={"auth_code": code, "code_verifier": verifier},
        )
        if res.status_code >= 400:
            logger.warning(f"OAuth code exchange failed ({res.status_code}): {res.text}")
            raise HTTPException(status_code=400, detail="OAuth code exchange failed")
        return self._to_auth_response(res)

    async def sign_in_with_password(
        self, email: EmailStr, password: str
    ) -> supabase_auth.AuthResponse:
        res = await self._gotrue(
            "POST",
            "token",
            params={"grant_type": "password"},
            json={"email": email, "password": password},
        )
        if res.status_code >= 400:
            # Normalise any GoTrue sign-in failure (bad creds, unconfirmed email) to 401.
            logger.warning(f"Sign in failed ({res.status_code}): {res.text}")
            raise HTTPException(status_code=401, detail="Invalid login credentials")
        auth_res = self._to_auth_response(res)
        if not auth_res.user or not auth_res.session:
            raise HTTPException(status_code=401, detail="User Session not found")
        return auth_res

    async def sign_up_with_password(
        self,
        email: EmailStr,
        password: str,
        redirect_to: None | str = None,
        data: dict | None = None,
    ) -> supabase_auth.AuthResponse:
        body: dict = {"email": email, "password": password}
        if data:
            # user_metadata (e.g. name / full_name), same as supabase-js signUp options.data
            body["data"] = data
        params = {"redirect_to": redirect_to} if redirect_to else None
        res = await self._gotrue("POST", "signup", params=params, json=body)
        if res.status_code >= 400:
            raise self._gotrue_error(res)
        auth_res = self._to_auth_response(res)
        if not auth_res.user:
            raise HTTPException(status_code=409, detail="System conflict while creating user")
        return auth_res

    async def verify_otp(self, params: dict) -> supabase_auth.AuthResponse:
        """Mirror of ``supabase.auth.verifyOtp`` — returns User + Session."""
        redirect_to = (params.get("options") or {}).get("redirect_to")
        body = {k: v for k, v in params.items() if k != "options"}
        res = await self._gotrue(
            "POST",
            "verify",
            params={"redirect_to": redirect_to} if redirect_to else None,
            json=body,
        )
        if res.status_code >= 400:
            raise self._gotrue_error(res, status=401)
        return self._to_auth_response(res)

    async def resend(self, credentials: dict) -> supabase_auth.AuthOtpResponse:
        """Mirror of ``supabase.auth.resend`` — resends a signup/email-change OTP."""
        redirect_to = (credentials.get("options") or {}).get("email_redirect_to")
        body = {k: v for k, v in credentials.items() if k != "options"}
        res = await self._gotrue(
            "POST",
            "resend",
            params={"redirect_to": redirect_to} if redirect_to else None,
            json=body,
        )
        if res.status_code >= 400:
            raise self._gotrue_error(res, status=400)
        data = res.json() if res.content else {}
        return supabase_auth.AuthOtpResponse(message_id=(data or {}).get("message_id"))

    async def sign_out_with_token(self, access_token: str) -> None:
        """
        Mirror of ``supabase.auth.signOut`` for a specific user token. Revokes the
        session at GoTrue with the caller's own token (there is no per-user session held
        server-side to sign out).
        """
        res = await self._gotrue("POST", "logout", token=access_token)
        # 204 = signed out; 401 = token already invalid/expired, which is fine.
        if res.status_code not in (204, 401):
            logger.warning(f"Sign out failed ({res.status_code}): {res.text}")
            raise HTTPException(status_code=400, detail="Sign out failed")

    async def update_user_with_token(
        self, access_token: str, attributes: dict
    ) -> supabase_auth.UserResponse:
        """
        Mirror of ``supabase.auth.updateUser`` — updates the caller's own user with the
        caller's token and returns the updated User. Stateless / multi-worker safe.
        """
        res = await self._gotrue("PUT", "user", token=access_token, json=attributes)
        if res.status_code >= 400:
            # Preserve the status (e.g. 422 weak-password) but don't echo GoTrue's body.
            logger.warning(f"Update user failed ({res.status_code}): {res.text}")
            raise HTTPException(status_code=res.status_code, detail="Update user failed")
        return supabase_auth.UserResponse(user=supabase_auth.User(**res.json()))

    async def get_user(self, access_token: str) -> supabase_auth.UserResponse:
        """Mirror of ``supabase.auth.getUser`` — validate a token at GoTrue and return
        its user. Stateless: the token is sent straight to GoTrue's ``GET /user``."""
        res = await self._gotrue("GET", "user", token=access_token)
        if res.status_code >= 400:
            raise self._gotrue_error(res, status=401)
        return supabase_auth.UserResponse(user=supabase_auth.User(**res.json()))

    # ------------------------------------------------------------------
    # Admin endpoints — authenticated with the service_role key.
    # ------------------------------------------------------------------

    async def _gotrue_admin(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: Any | None = None,
    ) -> httpx.Response:
        """One GoTrue REST call authenticated with the service_role key.

        Separate from ``_gotrue`` because admin endpoints reject the anon
        key and require the service_role (secret) bearer.
        """
        settings = SettingsProvider.sget()
        if not settings.SUPABASE_SERVICE_ROLE_KEY:
            raise HTTPException(
                status_code=500,
                detail="SUPABASE_SERVICE_ROLE_KEY is not configured",
            )
        headers = {
            "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "X-Supabase-Api-Version": _GOTRUE_API_VERSION,
        }
        url = f"{settings.SUPABASE_URL}/auth/v1/{path}"
        kwargs: dict = {"headers": headers}
        if params is not None:
            kwargs["params"] = params
        if json is not None:
            kwargs["json"] = json
        async with httpx.AsyncClient(timeout=_GOTRUE_TIMEOUT) as client:
            res = await getattr(client, method.lower())(url, **kwargs)
        served = res.headers.get("x-supabase-api-version")
        if served and served != _GOTRUE_API_VERSION:
            logger.warning(
                f"GoTrue API version drift (admin): requested {_GOTRUE_API_VERSION}, "
                f"served {served} (path=auth/v1/{path})."
            )
        return res

    async def list_users(
        self, page: int = 1, per_page: int = 200
    ) -> dict:
        """List users via GoTrue admin API (service_role).

        Returns the raw JSON page from ``GET /admin/users``:
            {"users": [...], "total": int, "page": int, "per_page": int}

        Each user dict has the same shape as ``supabase_auth.User``.
        """
        res = await self._gotrue_admin(
            "GET", "admin/users", params={"page": page, "per_page": per_page}
        )
        if res.status_code >= 400:
            raise self._gotrue_error(res)
        return res.json()

    async def get_user_by_id(self, user_id: str) -> supabase_auth.UserResponse:
        """Fetch a single user by ID via GoTrue admin API (service_role).

        Returns a ``supabase_auth.UserResponse`` so the caller can pass it
        directly to ``User.create_if_not_exist``.
        """
        res = await self._gotrue_admin("GET", f"admin/users/{user_id}")
        if res.status_code >= 400:
            raise self._gotrue_error(res, status=404)
        return supabase_auth.UserResponse(user=supabase_auth.User(**res.json()))

    async def reset_password_email(self, email: EmailStr, redirect_to: HttpUrl = None) -> bool:
        res = await self._gotrue(
            "POST",
            "recover",
            params={"redirect_to": str(redirect_to)} if redirect_to else None,
            json={"email": email},
        )
        if res.status_code >= 400:
            raise self._gotrue_error(res)
        return True

    async def refresh_session(self, refresh_token: str) -> supabase_auth.AuthResponse:
        res = await self._gotrue(
            "POST",
            "token",
            params={"grant_type": "refresh_token"},
            json={"refresh_token": refresh_token},
        )
        if res.status_code >= 400:
            raise self._gotrue_error(res, status=401)
        return self._to_auth_response(res)


@lru_cache
def get_auth() -> Auth:
    return Auth()
