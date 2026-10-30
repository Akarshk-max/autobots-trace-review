"""ASGI middleware for MCP per-request auth.

Wraps the FastMCP ASGI app and resolves the caller's identity in one of two ways:

1. ``Authorization: Bearer <jwt>`` — preferred. The token is verified offline
   (signature, audience, expiry) with no Supabase round-trip and no password
   anywhere in the request.
2. ``X-Autobots-Email`` + ``X-Autobots-Password`` — the bootstrap path, used to
   obtain a token in the first place.

The intended client flow is password-once, token-thereafter:

    POST /v1/mcp/auth  {"email": ..., "password": ...}
      -> {"access_token": ..., "token_type": "Bearer", "expires_in": 3540}
    ... every subsequent MCP call sends Authorization: Bearer <access_token>
    ... on 401 with X-Autobots-Auth-Error: token_invalid, exchange again.

Clients that cannot perform the exchange up front may keep sending the password
headers; the server refreshes the Supabase session on their behalf, so that path
never expires.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from contextvars import ContextVar

from loguru import logger
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from src.autobots.auth.providers.auth_provider_registry import get_auth_provider
from src.autobots.core.settings import SettingsProvider
from src.autobots.mcp_server.auth import MCPAuth
from src.autobots.mcp_server.client import APIClient

# Per-request contextvar holding the APIClient
_api_client_ctx: ContextVar[APIClient | None] = ContextVar("mcp_api_client", default=None)

# Path (relative to the /v1/mcp mount) of the credential-for-token exchange.
_AUTH_EXCHANGE_PATH = "/auth"

# Set on 401s so a client can tell "your token aged out, exchange again" apart
# from "your credentials are wrong", and retry only in the first case.
_AUTH_ERROR_HEADER = "x-autobots-auth-error"

# Failed-credential throttle. Bounds how fast one source can guess passwords, and
# how much traffic it can push at Supabase on our behalf. Only the credential path
# is counted — a bad Bearer token is rejected locally and costs nothing.
_FAILED_AUTH_MAX = 5
_FAILED_AUTH_WINDOW_SEC = 60
_FAILED_AUTH_MAX_TRACKED = 1024
_failed_auth: OrderedDict[str, list[float]] = OrderedDict()


def get_api_client() -> APIClient:
    """Retrieve the APIClient for the current MCP request."""
    c = _api_client_ctx.get()
    if c is None:
        raise RuntimeError("No APIClient in context – MCP middleware did not run")
    return c


# Token cache: (email, password digest) -> MCPAuth (avoids re-auth on every MCP call).
# The password MUST be part of the key: keying on email alone means that once any
# caller authenticates an address, every later request for that address reuses the
# cached session and its password is never checked.
#
# Ordered + LRU so that a stream of bad credentials cannot evict live sessions:
# entries are only ever added after Supabase has accepted the credentials, and a
# cache hit moves the entry to the end, so the entry dropped when the cache is
# full is the genuinely least-recently-used one.
_auth_cache: OrderedDict[tuple[str, str], MCPAuth] = OrderedDict()

# Bound the cache so a stream of distinct credentials cannot grow it without limit.
_AUTH_CACHE_MAX = 128


def _cache_key(email: str, password: str) -> tuple[str, str]:
    return (email, hashlib.sha256(password.encode("utf-8")).hexdigest())


async def _authenticate(email: str, password: str) -> MCPAuth:
    """Return an authenticated ``MCPAuth``, caching only *after* auth succeeds.

    Caching before authenticating (the previous behaviour) meant a wrong password
    still claimed an entry — one that could never hold a token, so every retry hit
    Supabase anyway — while pushing a working session out of a full cache. Bad
    credentials now leave no trace here at all.
    """
    key = _cache_key(email, password)
    auth = _auth_cache.get(key)
    if auth is not None:
        _auth_cache.move_to_end(key)
        await auth.ensure_fresh_token()
        return auth

    auth = MCPAuth(email, password)
    # Raises on bad credentials — nothing is cached in that case.
    await auth.ensure_fresh_token()

    if len(_auth_cache) >= _AUTH_CACHE_MAX:
        _auth_cache.popitem(last=False)
    _auth_cache[key] = auth
    return auth


def _bearer_token(headers: dict[str, str]) -> str | None:
    """Extract the raw JWT from an ``Authorization: Bearer <token>`` header."""
    raw = headers.get("authorization", "").strip()
    if not raw:
        return None
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def _relative_path(scope: Scope) -> str:
    """Path within the MCP mount.

    Starlette's ``Mount`` leaves ``scope["path"]`` as the full request path and
    reports the prefix separately in ``root_path``, so matching on ``path`` alone
    never sees "/auth" when the app is mounted at /v1/mcp. Stripping root_path
    also keeps this correct when the middleware is exercised unmounted.
    """
    path = scope.get("path", "")
    root = scope.get("root_path", "")
    if root and path.startswith(root):
        path = path[len(root) :]
    return path or "/"


def _client_ip(scope: Scope, headers: dict[str, str]) -> str:
    """Best-effort caller identity for throttling.

    Prefers the left-most X-Forwarded-For hop, since behind a load balancer the
    transport peer is the balancer itself and every caller would share one bucket.
    XFF is caller-supplied and therefore spoofable: this is a brake on naive
    guessing, not an access control, and nothing is authorised on the strength of it.
    """
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    client = scope.get("client")
    return client[0] if client else "unknown"


def _is_throttled(ip: str) -> bool:
    """True when `ip` has burned through its failed-credential budget."""
    now = time.monotonic()
    attempts = [t for t in _failed_auth.get(ip, []) if now - t < _FAILED_AUTH_WINDOW_SEC]
    if attempts:
        _failed_auth[ip] = attempts
        _failed_auth.move_to_end(ip)
    else:
        _failed_auth.pop(ip, None)
    return len(attempts) >= _FAILED_AUTH_MAX


def _record_auth_failure(ip: str) -> None:
    now = time.monotonic()
    attempts = [t for t in _failed_auth.get(ip, []) if now - t < _FAILED_AUTH_WINDOW_SEC]
    attempts.append(now)
    _failed_auth[ip] = attempts
    _failed_auth.move_to_end(ip)
    while len(_failed_auth) > _FAILED_AUTH_MAX_TRACKED:
        _failed_auth.popitem(last=False)


def _clear_auth_failures(ip: str) -> None:
    """A successful authentication resets the budget."""
    _failed_auth.pop(ip, None)


def _is_secure_transport(scope: Scope, headers: dict[str, str]) -> bool:
    """Whether credentials may be accepted on this connection.

    A password sent over cleartext HTTP is readable by every hop in between, so
    the credential path requires TLS. ``x-forwarded-proto`` is honoured because
    TLS normally terminates at the load balancer, leaving scope["scheme"] as
    "http" inside the cluster. Loopback and local/test runs are exempt so
    `make dev` keeps working.
    """
    if SettingsProvider.sget().ENV in ("local", "test"):
        return True

    client = scope.get("client")
    if client and client[0] in ("127.0.0.1", "::1", "localhost"):
        return True

    proto = headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    return scope.get("scheme") == "https"


async def _guarded_authenticate(
    scope: Scope, headers: dict[str, str], email: str, password: str
) -> tuple[MCPAuth | None, Response | None]:
    """Guard the credential path (TLS, then throttle) and authenticate.

    Returns ``(auth, None)`` on success or ``(None, response)`` to send instead.
    Shared by the exchange endpoint and the header fallback so neither can drift.
    """
    if not _is_secure_transport(scope, headers):
        logger.warning("MCP credentials refused over cleartext transport")
        return None, Response(
            content="Credentials require HTTPS. Use Authorization: Bearer over TLS.",
            status_code=400,
            media_type="text/plain",
            headers={_AUTH_ERROR_HEADER: "insecure_transport"},
        )

    ip = _client_ip(scope, headers)
    if _is_throttled(ip):
        logger.warning(f"MCP auth throttled for {ip}")
        return None, Response(
            content="Too many failed authentication attempts. Retry later.",
            status_code=429,
            media_type="text/plain",
            headers={
                _AUTH_ERROR_HEADER: "rate_limited",
                "retry-after": str(_FAILED_AUTH_WINDOW_SEC),
            },
        )

    try:
        auth = await _authenticate(email, password)
    except Exception as e:
        _record_auth_failure(ip)
        # Log the cause; do not echo it to the caller.
        logger.error(f"MCP auth failed for {email}: {type(e).__name__}: {e}")
        return None, _unauthorized("credentials_invalid", "Authentication failed")

    _clear_auth_failures(ip)
    return auth, None


def _unauthorized(reason: str, message: str) -> Response:
    """401 with a machine-readable reason and no detail echoed back to the caller."""
    return Response(
        content=message,
        status_code=401,
        media_type="text/plain",
        headers={_AUTH_ERROR_HEADER: reason},
    )


class MCPAuthMiddleware:
    """ASGI middleware: resolve the caller (Bearer token or credentials), set contextvar."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    @property
    def lifespan(self):
        """Forward the inner app's lifespan (needed for Starlette lifespan protocol)."""
        return getattr(self._app, "lifespan", None)

    @property
    def app(self) -> ASGIApp:
        """The wrapped MCP app.

        Exposed so whoever mounts this can run the MCP app's own lifespan against
        it: Starlette does not propagate lifespan into mounts, and FastMCP starts
        its StreamableHTTPSessionManager there.
        """
        return self._app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}

        # APP_BASE_URL defaults to "", which would otherwise fail deep inside httpx
        # and be reported as an authentication failure by the handler below.
        # A trailing slash is fine: APIClient rstrips it before building paths.
        base_url = SettingsProvider.sget().APP_BASE_URL
        if not base_url:
            logger.error("APP_BASE_URL is not set — MCP cannot reach the Autobots API")
            resp = Response(
                content="Server misconfigured: APP_BASE_URL is not set",
                status_code=500,
                media_type="text/plain",
            )
            await resp(scope, receive, send)
            return

        if _relative_path(scope).rstrip("/") == _AUTH_EXCHANGE_PATH:
            await self._handle_exchange(scope, receive, send, headers)
            return

        # --- Bearer path: no password, no Supabase round-trip --------------------
        token = _bearer_token(headers)
        if token:
            # Verifies signature, audience and expiry locally.
            if get_auth_provider().decode_payload_offline(token) is None:
                await _unauthorized(
                    "token_invalid",
                    "Bearer token is invalid or expired — exchange credentials at /v1/mcp/auth",
                )(scope, receive, send)
                return

            async def _get_token() -> str:
                return token

            _api_client_ctx.set(APIClient(base_url=base_url, get_token=_get_token))
            await self._app(scope, receive, send)
            return

        # --- Credential path: bootstrap, and hand back a token -------------------
        email = headers.get("x-autobots-email", "").strip()
        password = headers.get("x-autobots-password", "").strip()
        if not email or not password:
            await _unauthorized(
                "credentials_missing",
                "Send Authorization: Bearer <token>, or X-Autobots-Email and "
                "X-Autobots-Password to obtain one from /v1/mcp/auth",
            )(scope, receive, send)
            return

        auth, error = await _guarded_authenticate(scope, headers, email, password)
        if error is not None:
            await error(scope, receive, send)
            return

        # APIClient awaits get_token(), so pass the async method itself
        # (a plain lambda returning a str is not awaitable) — this also
        # lets long-lived sessions pick up a refreshed token.
        _api_client_ctx.set(APIClient(base_url=base_url, get_token=auth.ensure_fresh_token))
        await self._app(scope, receive, send)

    async def _handle_exchange(
        self, scope: Scope, receive: Receive, send: Send, headers: dict[str, str]
    ) -> None:
        """POST /v1/mcp/auth — trade email + password for a short-lived Bearer token."""
        if scope.get("method", "").upper() != "POST":
            await Response(
                content="Method not allowed", status_code=405, headers={"Allow": "POST"}
            )(scope, receive, send)
            return

        body = await _read_body(receive)
        try:
            payload = json.loads(body or b"{}")
            email = str(payload.get("email", "")).strip()
            password = str(payload.get("password", "")).strip()
        except Exception:
            await Response(content="Body must be JSON", status_code=400, media_type="text/plain")(
                scope, receive, send
            )
            return

        if not email or not password:
            await _unauthorized("credentials_missing", "Both 'email' and 'password' are required")(
                scope, receive, send
            )
            return

        auth, error = await _guarded_authenticate(scope, headers, email, password)
        if error is not None:
            await error(scope, receive, send)
            return

        await JSONResponse(
            {
                "access_token": auth.access_token,
                "token_type": "Bearer",
                "expires_in": auth.expires_in,
            }
        )(scope, receive, send)


async def _read_body(receive: Receive) -> bytes:
    """Drain an ASGI request body."""
    body = b""
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    return body
