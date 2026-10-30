import base64
import hashlib
import hmac
import time
import urllib.parse
import uuid
from typing import Optional

from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import RedirectResponse

from src.autobots import SettingsProvider
from src.autobots.auth.security import raise_if_redirect_uri_not_allowed
from src.autobots.data_model.context import Context

# The provider redirects the user's browser to our callback, so `state` is the only
# channel carrying user identity. It must be tamper-proof: without a signature anyone
# could forge a state and have a connection stored against another user's account.
_STATE_TTL_SECONDS = 600


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = len(value) % 4
    if padding:
        value += "=" * (4 - padding)
    return base64.urlsafe_b64decode(value)


def _sign(payload_b64: str) -> str:
    key = SettingsProvider.sget().JWT_SECRET_KEY.encode()
    return _b64url_encode(hmac.new(key, payload_b64.encode(), hashlib.sha256).digest())


class OAuthState(BaseModel):
    name: str
    # Where to land once the connection is stored; never sent to the provider.
    redirect_uri: str
    user_id: str = ""  # recovered in the callback, which has no bearer token
    trace_id: str = ""  # of the request that started the flow; the callback adopts it
    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()))
    code_verifier: Optional[str] = None  # PKCE S256 verifier; set for flows that require it
    iat: int = Field(default_factory=lambda: int(time.time()))

    def to_base64(self) -> str:
        """Serializes the state to a signed `<payload>.<signature>` string."""
        payload_b64 = _b64url_encode(self.model_dump_json().encode())
        return f"{payload_b64}.{_sign(payload_b64)}"

    @classmethod
    def from_base64(cls, state_b64: str) -> "OAuthState":
        """Deserializes and verifies the state. Raises 400 if forged or expired."""
        payload_b64, _, signature = state_b64.partition(".")
        if not signature or not hmac.compare_digest(signature, _sign(payload_b64)):
            raise HTTPException(status_code=400, detail="Invalid OAuth state signature")
        try:
            state = cls.model_validate_json(_b64url_decode(payload_b64).decode())
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid OAuth state")
        if time.time() - state.iat > _STATE_TTL_SECONDS:
            raise HTTPException(status_code=400, detail="OAuth state expired")
        return state


def new_oauth_state(
    ctx: Context, name: str, redirect_uri: str, code_verifier: str | None = None
) -> OAuthState:
    """Mint the state for a flow, carrying the caller and this request's trace_id."""
    return OAuthState(
        name=name,
        redirect_uri=redirect_uri,
        user_id=ctx.user_id or "",
        trace_id=ctx.trace_id,
        code_verifier=code_verifier,
    )


def resolve_oauth_state(ctx: Context, state: str | None) -> OAuthState:
    """
    Parse, verify and require a user-bearing state, then put the flow back on ctx.

    The callback is its own request, so the middleware handed it a fresh trace_id and no
    user. Both are taken from the state, which means the callback logs under the trace
    that started the flow instead of an unconnected one.
    """
    if not state:
        raise HTTPException(status_code=400, detail="Missing OAuth state")
    try:
        oauth_state = OAuthState.from_base64(state)
    except HTTPException as e:
        logger.bind(ctx=ctx).warning(f"Rejected OAuth state: {e.detail}")
        raise
    if not oauth_state.user_id:
        logger.bind(ctx=ctx).warning("Rejected OAuth state: missing user_id")
        raise HTTPException(status_code=400, detail="OAuth state missing user_id")
    ctx.user_id = oauth_state.user_id
    ctx.trace_id = oauth_state.trace_id or ctx.trace_id
    # States minted before this field existed keep the callback's own trace_id.
    return oauth_state


async def post_auth_redirect_uri(ctx: Context, redirect_uri: str | None) -> str:
    """
    Resolve and validate where the user lands once the connection is stored.

    Relative values (and no value at all) resolve against the first allowed origin,
    because raise_if_redirect_uri_not_allowed only understands absolute URLs.
    """
    candidate = (redirect_uri or "").strip()
    if not candidate or candidate.startswith("/"):
        allowed_origins = SettingsProvider.sget().ALLOWED_ORIGINS
        if not allowed_origins:
            raise HTTPException(status_code=500, detail="No allowed origin to redirect to")
        candidate = f"{allowed_origins[0].rstrip('/')}{candidate}"
    try:
        _ = await raise_if_redirect_uri_not_allowed(ctx=ctx, redirect_uri=candidate)
    except HTTPException:
        logger.bind(ctx=ctx).warning(f"Post-auth redirect not allowed: {candidate}")
        raise
    return candidate


_LOCAL_HOSTS = ("localhost", "127.0.0.1")


def build_backend_callback_url(ctx: Context, request: Request, callback_path: str) -> str:
    """
    Absolute URL of a backend OAuth callback, e.g. "/v1/auth/github/oauth-callback".

    Taken from the request, so no environment variable has to be set for a deployment to
    complete OAuth — the provider only needs this exact URL registered in its console.

    The scheme is not request.url.scheme: uvicorn believes X-Forwarded-Proto only from a
    127.0.0.1 peer, so on an ingress where the proxy is a separate hop (dev.api.
    myautobots.com) the scope says http while the browser used https — and a provider
    matches the registered URI byte-for-byte.
    """
    host = request.url.netloc
    scheme = request.url.scheme if host.split(":")[0] in _LOCAL_HOSTS else "https"
    callback_url = f"{scheme}://{host}{callback_path}"
    logger.bind(ctx=ctx).info(f"OAuth callback URL: {callback_url}")
    return callback_url


def oauth_error_redirect(ctx: Context, redirect_uri: str, error: Exception | str) -> RedirectResponse:
    """
    Send the browser back to the app with the failure attached.

    A callback is reached by a browser, not an API client: raising here strands the user
    on an error page outside the app instead of back where they started.
    """
    # HTTPException stringifies as "400: access_denied"; the app only wants the reason.
    detail = error.detail if isinstance(error, HTTPException) else error
    logger.bind(ctx=ctx).warning(f"OAuth failed, returning the user to {redirect_uri}: {detail}")
    separator = "&" if "?" in redirect_uri else "?"
    quoted = urllib.parse.quote_plus(str(detail))
    return RedirectResponse(f"{redirect_uri}{separator}oauth_error={quoted}", status_code=303)
