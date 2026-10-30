"""End-to-end auth-flow tests for MCPAuthMiddleware.

Covers the password-once / token-thereafter handshake:
password -> /v1/mcp/auth -> Bearer token -> MCP calls -> 401 on expiry -> repeat.
"""

import json

import pytest

from src.autobots.mcp_server import middleware


class _StubAuth:
    def __init__(self, email: str, password: str) -> None:
        self.email = email
        self.password = password
        self._access_token = "jwt-for-" + email

    async def ensure_fresh_token(self) -> str:
        if self.password != "correct-horse":
            raise RuntimeError("invalid login credentials")
        return self._access_token

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def expires_in(self) -> int:
        return 3540


class _StubAuthProvider:
    """Accepts any token except ones marked expired/forged."""

    def decode_payload_offline(self, token: str, audience: str = "authenticated"):
        return None if token in ("expired-jwt", "forged-jwt") else {"sub": "user-id"}


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    middleware._auth_cache.clear()
    monkeypatch.setattr(middleware, "MCPAuth", _StubAuth)
    monkeypatch.setattr(middleware, "get_auth_provider", lambda: _StubAuthProvider())

    class _Settings:
        APP_BASE_URL = "https://api.example.com"
        ENV = "test"  # exempt from the TLS requirement; prod_env overrides this

    monkeypatch.setattr(middleware.SettingsProvider, "sget", staticmethod(lambda: _Settings()))
    middleware._failed_auth.clear()
    yield
    middleware._auth_cache.clear()
    middleware._failed_auth.clear()


class _Downstream:
    """Inner ASGI app: records the APIClient the middleware put in the contextvar."""

    def __init__(self):
        self.called = False
        self.token = None

    async def __call__(self, scope, receive, send):
        self.called = True
        client = middleware.get_api_client()
        self.token = await client._get_token()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _call(app, path="/", method="POST", headers=None, body=b""):
    """Drive an ASGI app and collect the response."""
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": method, "path": path, "headers": raw}

    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)

    start = next(m for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    resp_headers = {k.decode().lower(): v.decode() for k, v in start.get("headers", [])}
    return start["status"], resp_headers, payload


# --- the exchange endpoint --------------------------------------------------


@pytest.mark.asyncio
async def test_exchange_trades_credentials_for_a_bearer_token():
    status, _, body = await _call(
        middleware.MCPAuthMiddleware(_Downstream()),
        path="/auth",
        body=json.dumps({"email": "user@example.com", "password": "correct-horse"}).encode(),
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["access_token"] == "jwt-for-user@example.com"
    assert payload["token_type"] == "Bearer"
    assert payload["expires_in"] == 3540


@pytest.mark.asyncio
async def test_exchange_does_not_reach_the_mcp_app():
    downstream = _Downstream()
    await _call(
        middleware.MCPAuthMiddleware(downstream),
        path="/auth",
        body=json.dumps({"email": "user@example.com", "password": "correct-horse"}).encode(),
    )
    assert not downstream.called, "the exchange endpoint must not fall through to MCP"


@pytest.mark.asyncio
async def test_exchange_rejects_bad_credentials_without_echoing_the_reason():
    status, headers, body = await _call(
        middleware.MCPAuthMiddleware(_Downstream()),
        path="/auth",
        body=json.dumps({"email": "user@example.com", "password": "nope"}).encode(),
    )
    assert status == 401
    assert headers[middleware._AUTH_ERROR_HEADER] == "credentials_invalid"
    assert b"invalid login credentials" not in body, "upstream detail must not leak to the caller"


# --- the Bearer path --------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_token_is_accepted_and_forwarded_without_touching_supabase():
    downstream = _Downstream()
    status, _, _ = await _call(
        middleware.MCPAuthMiddleware(downstream),
        headers={"Authorization": "Bearer good-jwt"},
    )
    assert status == 200
    assert downstream.token == "good-jwt"
    assert len(middleware._auth_cache) == 0, "the Bearer path must not authenticate at all"


@pytest.mark.asyncio
async def test_expired_token_is_rejected_with_a_retryable_reason():
    downstream = _Downstream()
    status, headers, _ = await _call(
        middleware.MCPAuthMiddleware(downstream),
        headers={"Authorization": "Bearer expired-jwt"},
    )
    assert status == 401
    assert headers[middleware._AUTH_ERROR_HEADER] == "token_invalid"
    assert not downstream.called


@pytest.mark.asyncio
async def test_bearer_is_preferred_over_credential_headers():
    """A client that sends both must not trigger a password authentication."""
    downstream = _Downstream()
    await _call(
        middleware.MCPAuthMiddleware(downstream),
        headers={
            "Authorization": "Bearer good-jwt",
            "X-Autobots-Email": "user@example.com",
            "X-Autobots-Password": "correct-horse",
        },
    )
    assert downstream.token == "good-jwt"
    assert len(middleware._auth_cache) == 0


# --- the credential fallback ------------------------------------------------


@pytest.mark.asyncio
async def test_missing_credentials_are_rejected():
    status, headers, _ = await _call(middleware.MCPAuthMiddleware(_Downstream()))
    assert status == 401
    assert headers[middleware._AUTH_ERROR_HEADER] == "credentials_missing"


@pytest.mark.asyncio
async def test_downstream_errors_are_not_reported_as_auth_failures():
    """The auth try/except used to wrap the MCP app, turning any tool crash into a 401."""

    class _Boom:
        async def __call__(self, scope, receive, send):
            raise RuntimeError("tool exploded")

    with pytest.raises(RuntimeError, match="tool exploded"):
        await _call(
            middleware.MCPAuthMiddleware(_Boom()),
            headers={
                "X-Autobots-Email": "user@example.com",
                "X-Autobots-Password": "correct-horse",
            },
        )


# --- mounted for real -------------------------------------------------------
# Starlette's Mount leaves scope["path"] as the *full* path and reports the
# prefix in root_path, so matching on scope["path"] alone never sees "/auth"
# once the app is mounted at /v1/mcp. Only a mounted test catches that.


@pytest.fixture
def mounted():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    downstream = _Downstream()
    app = FastAPI()
    app.mount("/v1/mcp", middleware.MCPAuthMiddleware(downstream))
    return TestClient(app), downstream


def test_exchange_is_reachable_at_the_mounted_path(mounted):
    client, downstream = mounted
    res = client.post(
        "/v1/mcp/auth", json={"email": "user@example.com", "password": "correct-horse"}
    )
    assert res.status_code == 200, "the /auth path must survive the mount prefix"
    assert res.json()["access_token"] == "jwt-for-user@example.com"
    assert not downstream.called


def test_full_handshake_through_the_mount(mounted):
    """password -> token -> authenticated MCP call, exactly as a client would."""
    client, downstream = mounted

    token = client.post(
        "/v1/mcp/auth", json={"email": "user@example.com", "password": "correct-horse"}
    ).json()["access_token"]

    res = client.post("/v1/mcp/", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert downstream.token == token
    assert len(middleware._auth_cache) == 1, "the token call must not re-authenticate"


def test_mcp_calls_still_reach_the_app_when_mounted(mounted):
    client, downstream = mounted
    res = client.post(
        "/v1/mcp/",
        headers={"X-Autobots-Email": "user@example.com", "X-Autobots-Password": "correct-horse"},
    )
    assert res.status_code == 200
    assert downstream.called


# --- transport + throttle guards --------------------------------------------


@pytest.fixture
def prod_env(monkeypatch):
    """Leave the local/test exemption so the guards are actually exercised."""

    class _Settings:
        APP_BASE_URL = "https://api.example.com"
        ENV = "prod"

    monkeypatch.setattr(middleware.SettingsProvider, "sget", staticmethod(lambda: _Settings()))
    middleware._failed_auth.clear()
    yield
    middleware._failed_auth.clear()


def _creds(**extra):
    return {
        "X-Autobots-Email": "user@example.com",
        "X-Autobots-Password": "correct-horse",
        **extra,
    }


@pytest.mark.asyncio
async def test_credentials_are_refused_over_cleartext(prod_env):
    downstream = _Downstream()
    status, headers, _ = await _call(
        middleware.MCPAuthMiddleware(downstream),
        headers=_creds(**{"X-Forwarded-Proto": "http"}),
    )
    assert status == 400
    assert headers[middleware._AUTH_ERROR_HEADER] == "insecure_transport"
    assert not downstream.called


@pytest.mark.asyncio
async def test_credentials_are_accepted_behind_a_tls_terminating_proxy(prod_env):
    """TLS ends at the load balancer, so scope["scheme"] is http inside the cluster."""
    downstream = _Downstream()
    status, _, _ = await _call(
        middleware.MCPAuthMiddleware(downstream),
        headers=_creds(**{"X-Forwarded-Proto": "https"}),
    )
    assert status == 200
    assert downstream.called


@pytest.mark.asyncio
async def test_bearer_still_works_over_cleartext(prod_env):
    """A token is not a reusable secret the way a password is; only creds need TLS."""
    downstream = _Downstream()
    status, _, _ = await _call(
        middleware.MCPAuthMiddleware(downstream),
        headers={"Authorization": "Bearer good-jwt", "X-Forwarded-Proto": "http"},
    )
    assert status == 200
    assert downstream.token == "good-jwt"


@pytest.mark.asyncio
async def test_repeated_bad_credentials_are_throttled(prod_env):
    app = middleware.MCPAuthMiddleware(_Downstream())
    bad = _creds(**{"X-Autobots-Password": "guess", "X-Forwarded-Proto": "https"})

    for _ in range(middleware._FAILED_AUTH_MAX):
        status, headers, _ = await _call(app, headers=bad)
        assert status == 401
        assert headers[middleware._AUTH_ERROR_HEADER] == "credentials_invalid"

    status, headers, _ = await _call(app, headers=bad)
    assert status == 429
    assert headers[middleware._AUTH_ERROR_HEADER] == "rate_limited"
    assert headers["retry-after"] == str(middleware._FAILED_AUTH_WINDOW_SEC)


@pytest.mark.asyncio
async def test_throttle_is_per_source(prod_env):
    app = middleware.MCPAuthMiddleware(_Downstream())
    bad = _creds(**{"X-Autobots-Password": "guess", "X-Forwarded-Proto": "https"})

    for _ in range(middleware._FAILED_AUTH_MAX + 1):
        await _call(app, headers={**bad, "X-Forwarded-For": "10.0.0.1"})

    # A different source must be unaffected by the first one's budget.
    status, _, _ = await _call(
        app,
        headers=_creds(**{"X-Forwarded-Proto": "https", "X-Forwarded-For": "10.0.0.2"}),
    )
    assert status == 200


@pytest.mark.asyncio
async def test_successful_auth_clears_the_failure_budget(prod_env):
    app = middleware.MCPAuthMiddleware(_Downstream())
    src = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "10.0.0.3"}

    for _ in range(middleware._FAILED_AUTH_MAX - 1):
        await _call(app, headers=_creds(**{"X-Autobots-Password": "guess", **src}))

    assert (await _call(app, headers=_creds(**src)))[0] == 200
    assert "10.0.0.3" not in middleware._failed_auth, "a good login must reset the count"


@pytest.mark.asyncio
async def test_exchange_endpoint_is_throttled_too(prod_env):
    app = middleware.MCPAuthMiddleware(_Downstream())
    body = json.dumps({"email": "user@example.com", "password": "guess"}).encode()
    hdrs = {"X-Forwarded-Proto": "https"}

    for _ in range(middleware._FAILED_AUTH_MAX):
        assert (await _call(app, path="/auth", headers=hdrs, body=body))[0] == 401

    status, headers, _ = await _call(app, path="/auth", headers=hdrs, body=body)
    assert status == 429
    assert headers[middleware._AUTH_ERROR_HEADER] == "rate_limited"


# --- the mounted-lifespan contract ------------------------------------------
# Starlette does not run a mounted app's lifespan, so main.py runs the MCP app's
# lifespan itself. If that lifespan ever goes missing, FastMCP's session manager
# never starts: the process boots looking healthy and then fails EVERY /v1/mcp
# request with "Task group is not initialized". main.py now raises at startup
# instead, but this guards the contract it relies on.


def test_mcp_app_is_reachable_for_lifespan(monkeypatch):
    monkeypatch.delenv("DD_TRACE_ENABLED", raising=False)
    from src.autobots.mcp_server import create_mcp_server

    server = create_mcp_server()
    assert hasattr(server, "app"), "main.py needs a public handle on the wrapped MCP app"
    assert callable(getattr(server.app, "lifespan", None)), (
        "the mounted MCP app must expose a callable lifespan — without it the "
        "session manager never starts and every MCP request fails at runtime"
    )


def test_middleware_forwards_the_inner_lifespan():
    inner = _Downstream()
    inner.lifespan = "sentinel"
    assert middleware.MCPAuthMiddleware(inner).lifespan == "sentinel"
