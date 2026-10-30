import base64
import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException

from src.autobots.api.v1.api_auth.api_auth import _cli_callback_page, _cli_paste_token
from src.autobots.auth import auth as auth_module
from src.autobots.auth.auth import Auth


def _decode_token(token: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


def _s256_challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


@pytest.mark.asyncio
async def test_oauth_authorize_url_returns_verifier_and_builds_pkce_url():
    # Stateless: nothing is stored server-side; the verifier is returned to the caller.
    res = await Auth().oauth_authorize_url("google", "http://localhost:8976/callback")

    assert res.verifier  # returned to the CLI, not stored anywhere

    parsed = urlparse(res.url)
    assert parsed.path == "/auth/v1/authorize"
    q = parse_qs(parsed.query)
    assert q["provider"] == ["google"]
    assert q["redirect_to"] == ["http://localhost:8976/callback"]
    # We must NOT send our own state: it's reserved and managed by GoTrue. Overriding it
    # makes GoTrue reject its own callback (bad_oauth_state). PKCE provides the binding.
    assert "state" not in q
    assert q["code_challenge_method"] == ["s256"]
    # The challenge in the URL must be the S256 hash of the returned verifier.
    assert q["code_challenge"] == [_s256_challenge(res.verifier)]
    # Google needs offline access to hand back a refresh token.
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]


@pytest.mark.asyncio
async def test_oauth_authorize_url_azure_requests_scopes():
    res = await Auth().oauth_authorize_url("azure", "http://localhost:8976/callback")

    q = parse_qs(urlparse(res.url).query)
    assert q["scopes"] == ["email openid profile"]
    assert "access_type" not in q


def _mock_httpx(status_code: int, *, json=None, text=""):
    http_res = MagicMock(status_code=status_code, text=text)
    if json is not None:
        http_res.json.return_value = json
    client = AsyncMock()
    client.post.return_value = http_res
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    return client


@pytest.mark.asyncio
async def test_exchange_oauth_code_sends_verifier_and_returns_auth_response():
    session_json = {
        "access_token": "acc",
        "refresh_token": "ref",
        "expires_in": 3600,
        "expires_at": 9999999999,
        "token_type": "bearer",
        "user": {
            "id": "00000000-0000-0000-0000-000000000000",
            "aud": "authenticated",
            "app_metadata": {},
            "user_metadata": {},
            "created_at": "2026-01-01T00:00:00Z",
        },
    }
    client = _mock_httpx(200, json=session_json)

    with patch.object(auth_module.httpx, "AsyncClient", return_value=client):
        res = await Auth().exchange_oauth_code("the-code", "the-verifier")

    # Exchanged against the PKCE token endpoint with the caller-supplied verifier.
    assert client.post.call_args.args[0].endswith("/auth/v1/token")
    assert client.post.call_args.kwargs["params"] == {"grant_type": "pkce"}
    assert client.post.call_args.kwargs["json"] == {
        "auth_code": "the-code",
        "code_verifier": "the-verifier",
    }
    assert res.session.access_token == "acc"
    assert res.user.id == session_json["user"]["id"]


@pytest.mark.asyncio
async def test_exchange_oauth_code_provider_error_raises_400_without_body_leak():
    client = _mock_httpx(400, text="invalid_grant SENSITIVE_BODY")

    with patch.object(auth_module.httpx, "AsyncClient", return_value=client):
        with pytest.raises(HTTPException) as exc:
            await Auth().exchange_oauth_code("bad-code", "the-verifier")
    assert exc.value.status_code == 400
    # GoTrue's raw body must not leak to the client.
    assert "SENSITIVE_BODY" not in str(exc.value.detail)


# --- Headless paste-back landing page -------------------------------------------


def test_cli_paste_token_wraps_code_not_verifier():
    token = _cli_paste_token("the-code")
    payload = _decode_token(token)
    assert payload == {"code": "the-code"}
    # The PKCE verifier must never travel through the browser.
    assert "verifier" not in payload
    # base64url alphabet only — inherently HTML-safe.
    assert all(c.isalnum() or c in "-_" for c in token)


def test_cli_callback_page_renders_token_and_never_the_raw_code():
    # A hostile code with HTML metacharacters must not break out of the page.
    page = _cli_callback_page(code="<script>x</script>", error=None)
    token = _cli_paste_token("<script>x</script>")
    assert token in page
    assert "<script>x</script>" not in page  # raw code only ever appears base64-encoded
    # The CLI recovers exactly the code from what's shown.
    assert _decode_token(token) == {"code": "<script>x</script>"}


def test_cli_callback_page_error_path_is_escaped():
    page = _cli_callback_page(code=None, error="access_denied <b>")
    assert "Sign in failed" in page
    assert "<b>" not in page  # error text is HTML-escaped
