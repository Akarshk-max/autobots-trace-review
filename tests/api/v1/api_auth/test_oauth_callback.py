"""The OAuth callback is reached by the provider's browser redirect, not by the UI.

GitHub stands in for all 26 providers here — they share one shape: `/oauth-url` sends
the provider a fixed backend callback and hides the app-side destination inside signed
state; `/oauth-callback` takes no bearer token, recovers the user from that state, and
redirects the browser back to the app once the connection is stored.
"""

from __future__ import annotations

import inspect
import urllib.parse
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.autobots import SettingsProvider
from src.autobots.api.v1.api_auth import api_github_auth
from src.autobots.api.v1.api_auth.oauth_state import OAuthState
from src.autobots.integrations.integration_model import OAuthModel
from src.autobots.data_model.context import Context

ALLOWED = "https://app.example.com/"
LANDING = "https://app.example.com/connections"
CALLBACK = "https://api.example.com/v1/auth/github/oauth-callback"
# A test deployment still has the old UI-side value in its env; we must ignore it.
STALE_ENV_REDIRECT = "https://app.example.com/oauth/github"
USER_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _settings():
    settings = SettingsProvider.sget()
    previous = (
        settings.JWT_SECRET_KEY,
        settings.ALLOWED_ORIGINS,
        settings.KEY_GITHUB_REDIRECT_URL,
        settings.KEY_GITHUB_CLIENT_ID,
        settings.KEY_GITHUB_CLIENT_SECRET,
    )
    settings.JWT_SECRET_KEY = "test-signing-key"
    settings.ALLOWED_ORIGINS = [ALLOWED]
    settings.KEY_GITHUB_REDIRECT_URL = STALE_ENV_REDIRECT
    settings.KEY_GITHUB_CLIENT_ID = "client-id"
    settings.KEY_GITHUB_CLIENT_SECRET = "client-secret"
    yield settings
    (
        settings.JWT_SECRET_KEY,
        settings.ALLOWED_ORIGINS,
        settings.KEY_GITHUB_REDIRECT_URL,
        settings.KEY_GITHUB_CLIENT_ID,
        settings.KEY_GITHUB_CLIENT_SECRET,
    ) = previous


def _request():
    return SimpleNamespace(
        state=SimpleNamespace(context=Context()),
        url=SimpleNamespace(scheme="https", netloc="api.example.com"),
        headers={},
    )


def _user_res():
    return SimpleNamespace(user=SimpleNamespace(id=USER_ID))


def _state(**overrides) -> str:
    fields = {"name": "my-github", "redirect_uri": LANDING, "user_id": USER_ID}
    fields.update(overrides)
    return OAuthState(**fields).to_base64()


@pytest.fixture
def stored(monkeypatch):
    """Capture what the callback would persist, and what it sent to GitHub."""
    calls = SimpleNamespace(token_requests=[], integrations=[])

    async def _create_auth(ctx, app_creds):
        calls.token_requests.append(app_creds)
        return OAuthModel(access_token="gho_token", expires_in=3600)

    async def _create(ctx, db, user, integration_create, decrypt=False):
        calls.integrations.append((user, integration_create))
        return SimpleNamespace(id="app-auth-id", app_name=integration_create.app_name)

    monkeypatch.setattr(api_github_auth.GitHubAuth, "create_auth", _create_auth)
    monkeypatch.setattr(api_github_auth.UserIntegration, "create", _create)
    return calls


def test_the_callback_takes_no_bearer_token():
    """A browser arriving from GitHub carries no Authorization header."""
    params = inspect.signature(api_github_auth.github_callback).parameters

    assert "user_res" not in params
    assert "user_res" in inspect.signature(api_github_auth.github_oauth_url).parameters


@pytest.mark.asyncio
async def test_oauth_url_sends_github_the_backend_callback_and_hides_the_destination():
    res = await api_github_auth.github_oauth_url(
        request=_request(), name="my-github", redirect_uri=LANDING, user_res=_user_res()
    )

    query = urllib.parse.parse_qs(urllib.parse.urlparse(str(res.url)).query)
    # Derived from the request — the stale *_REDIRECT_URL env value is not consulted.
    assert query["redirect_uri"] == [CALLBACK]

    state = OAuthState.from_base64(query["state"][0])
    assert state.user_id == USER_ID
    assert state.redirect_uri == LANDING
    assert LANDING not in str(res.url).replace(query["state"][0], "")


@pytest.mark.asyncio
async def test_a_valid_state_stores_the_connection_and_lands_back_in_the_app(stored):
    res = await api_github_auth.github_callback(
        request=_request(), code="auth-code", state=_state(), db=None
    )

    assert res.status_code == 303
    assert res.headers["location"] == LANDING

    user, integration_create = stored.integrations[0]
    assert str(user.id) == USER_ID
    assert integration_create.name == "my-github"

    # GitHub validates the exchange redirect_uri against the registered one, so it must
    # be the backend callback — never the app-side destination from state.
    assert stored.token_requests[0].redirect_uri == CALLBACK


@pytest.mark.asyncio
async def test_the_callback_logs_under_the_trace_that_started_the_flow(stored):
    request = _request()
    await api_github_auth.github_oauth_url(
        request=request, name="my-github", redirect_uri=LANDING, user_res=_user_res()
    )
    flow_trace = request.state.context.trace_id

    callback_request = _request()  # the provider's redirect is a separate request
    assert callback_request.state.context.trace_id != flow_trace

    await api_github_auth.github_callback(
        request=callback_request, code="auth-code", state=_state(trace_id=flow_trace), db=None
    )

    assert callback_request.state.context.trace_id == flow_trace
    assert callback_request.state.context.user_id == USER_ID


@pytest.mark.asyncio
async def test_a_forged_state_stores_nothing(stored):
    payload = _state(user_id="victim-user-id").partition(".")[0]
    signature = _state().partition(".")[2]

    with pytest.raises(HTTPException) as exc:
        await api_github_auth.github_callback(
            request=_request(), code="auth-code", state=f"{payload}.{signature}", db=None
        )

    assert exc.value.status_code == 400
    assert stored.integrations == []


@pytest.mark.asyncio
async def test_a_provider_error_returns_the_user_to_the_app(stored):
    res = await api_github_auth.github_callback(
        request=_request(), code=None, state=_state(), error="access_denied", db=None
    )

    assert res.status_code == 303
    assert res.headers["location"] == f"{LANDING}?oauth_error=access_denied"
    assert stored.integrations == []


@pytest.mark.asyncio
async def test_a_foreign_destination_in_state_is_refused(stored):
    with pytest.raises(HTTPException) as exc:
        await api_github_auth.github_callback(
            request=_request(),
            code="auth-code",
            state=_state(redirect_uri="https://evil.example.net/steal"),
            db=None,
        )

    assert exc.value.status_code == 400
    assert stored.integrations == []
