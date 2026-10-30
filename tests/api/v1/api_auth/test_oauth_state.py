"""The OAuth `state` is the only thing carrying user identity into a callback.

The provider redirects the user's browser to us with no Authorization header, so a
state that can be forged is an account takeover: an attacker mints one naming the
victim's user_id and their connection lands in the victim's account.
"""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from src.autobots import SettingsProvider
from src.autobots.api.v1.api_auth.oauth_state import (
    OAuthState,
    build_backend_callback_url,
    new_oauth_state,
    oauth_error_redirect,
    post_auth_redirect_uri,
    resolve_oauth_state,
)
from src.autobots.data_model.context import Context

CTX = Context(user_id="caller")
ALLOWED = "https://app.example.com/"
USER_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _settings():
    settings = SettingsProvider.sget()
    previous = (settings.JWT_SECRET_KEY, settings.ALLOWED_ORIGINS)
    settings.JWT_SECRET_KEY = "test-signing-key"
    settings.ALLOWED_ORIGINS = [ALLOWED]
    yield settings
    settings.JWT_SECRET_KEY, settings.ALLOWED_ORIGINS = previous


def _state(**overrides) -> OAuthState:
    fields = {"name": "my-conn", "redirect_uri": f"{ALLOWED}connections", "user_id": USER_ID}
    fields.update(overrides)
    return OAuthState(**fields)


def test_round_trip_preserves_the_payload():
    restored = OAuthState.from_base64(_state(code_verifier="pkce-verifier").to_base64())

    assert restored.name == "my-conn"
    assert restored.redirect_uri == f"{ALLOWED}connections"
    assert restored.user_id == USER_ID
    assert restored.code_verifier == "pkce-verifier"


def test_a_tampered_payload_is_rejected():
    payload, _, signature = _state().to_base64().partition(".")
    forged = _state(user_id="victim-user-id").to_base64().partition(".")[0]

    with pytest.raises(HTTPException) as exc:
        OAuthState.from_base64(f"{forged}.{signature}")
    assert exc.value.status_code == 400


def test_an_unsigned_state_is_rejected():
    payload = _state().to_base64().partition(".")[0]

    with pytest.raises(HTTPException):
        OAuthState.from_base64(payload)


def test_a_state_signed_with_another_key_is_rejected(_settings):
    state_b64 = _state().to_base64()
    _settings.JWT_SECRET_KEY = "a-different-key"

    with pytest.raises(HTTPException):
        OAuthState.from_base64(state_b64)


def test_an_expired_state_is_rejected():
    stale = _state(iat=int(time.time()) - 3600).to_base64()

    with pytest.raises(HTTPException) as exc:
        OAuthState.from_base64(stale)
    assert "expired" in str(exc.value.detail).lower()


def test_resolve_requires_a_state_carrying_a_user():
    with pytest.raises(HTTPException):
        resolve_oauth_state(ctx=CTX, state=None)

    with pytest.raises(HTTPException):
        resolve_oauth_state(ctx=CTX, state=_state(user_id="").to_base64())

    assert resolve_oauth_state(ctx=CTX, state=_state().to_base64()).user_id == USER_ID


def test_a_new_state_carries_the_caller_and_the_trace_of_this_request():
    ctx = Context(user_id=USER_ID)

    state = new_oauth_state(ctx=ctx, name="my-conn", redirect_uri=f"{ALLOWED}connections")

    assert state.user_id == USER_ID
    assert state.trace_id == ctx.trace_id


def test_resolve_puts_the_flow_back_on_the_callback_context():
    """The callback is its own request: the middleware gave it a fresh trace and no user."""
    starting = Context(user_id=USER_ID)
    state_b64 = new_oauth_state(
        ctx=starting, name="my-conn", redirect_uri=f"{ALLOWED}connections"
    ).to_base64()

    callback_ctx = Context()
    assert callback_ctx.trace_id != starting.trace_id

    resolve_oauth_state(ctx=callback_ctx, state=state_b64)

    assert callback_ctx.trace_id == starting.trace_id
    assert callback_ctx.user_id == USER_ID


def test_a_state_minted_before_trace_ids_keeps_the_callbacks_own_trace():
    callback_ctx = Context()
    own_trace = callback_ctx.trace_id

    resolve_oauth_state(ctx=callback_ctx, state=_state().to_base64())

    assert callback_ctx.trace_id == own_trace
    assert callback_ctx.user_id == USER_ID


@pytest.mark.asyncio
async def test_post_auth_redirect_accepts_an_allowed_origin():
    assert await post_auth_redirect_uri(ctx=CTX, redirect_uri=f"{ALLOWED}connections") == f"{ALLOWED}connections"


@pytest.mark.asyncio
async def test_post_auth_redirect_resolves_relative_and_empty_values():
    # raise_if_redirect_uri_not_allowed only understands absolute URLs, so "/" used to 403.
    assert await post_auth_redirect_uri(ctx=CTX, redirect_uri="/connections") == "https://app.example.com/connections"
    assert await post_auth_redirect_uri(ctx=CTX, redirect_uri="") == "https://app.example.com"
    assert await post_auth_redirect_uri(ctx=CTX, redirect_uri=None) == "https://app.example.com"


@pytest.mark.asyncio
async def test_post_auth_redirect_refuses_a_foreign_origin():
    with pytest.raises(HTTPException) as exc:
        await post_auth_redirect_uri(ctx=CTX, redirect_uri="https://evil.example.net/steal")
    assert exc.value.status_code == 403


def _request(netloc: str, scheme: str = "https"):
    return type(
        "R", (), {"url": type("U", (), {"scheme": scheme, "netloc": netloc})()}
    )()


def test_backend_callback_url_comes_from_the_request_not_the_environment():
    """No env var has to be set for a deployment to complete OAuth."""
    url = build_backend_callback_url(
        ctx=CTX,
        request=_request("api.example.com"),
        callback_path="/v1/auth/github/oauth-callback",
    )

    assert url == "https://api.example.com/v1/auth/github/oauth-callback"


def test_backend_callback_url_is_https_when_the_scope_says_http():
    """
    TLS terminates at the proxy and uvicorn believes X-Forwarded-Proto only from a
    127.0.0.1 peer, so dev.api.myautobots.com hands the app an http scope for a request
    the browser made over https. A provider matches the registered URI byte-for-byte.
    """
    url = build_backend_callback_url(
        ctx=CTX,
        request=_request("dev.api.myautobots.com", scheme="http"),
        callback_path="/v1/auth/google/oauth-callback",
    )

    assert url == "https://dev.api.myautobots.com/v1/auth/google/oauth-callback"


@pytest.mark.parametrize("netloc", ["localhost:8000", "127.0.0.1:8000"])
def test_backend_callback_url_stays_http_for_local_development(netloc):
    url = build_backend_callback_url(
        ctx=CTX,
        request=_request(netloc, scheme="http"),
        callback_path="/v1/auth/github/oauth-callback",
    )

    assert url == f"http://{netloc}/v1/auth/github/oauth-callback"


def test_error_redirect_keeps_existing_query_parameters():
    plain = oauth_error_redirect(
        ctx=CTX, redirect_uri="https://app.example.com/connections", error=ValueError("nope")
    )
    assert plain.status_code == 303
    assert plain.headers["location"] == "https://app.example.com/connections?oauth_error=nope"

    with_query = oauth_error_redirect(
        ctx=CTX, redirect_uri="https://app.example.com/c?tab=apps", error="access_denied"
    )
    assert with_query.headers["location"] == (
        "https://app.example.com/c?tab=apps&oauth_error=access_denied"
    )
