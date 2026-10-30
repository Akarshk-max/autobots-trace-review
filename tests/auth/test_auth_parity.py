"""Parity of the /auth backend surface with the supabase-js methods autobots-web uses.

These are pure unit tests: the supabase client and GoTrue HTTP calls are mocked,
so they need neither a network nor a database.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import supabase_auth
from fastapi import HTTPException

from src.autobots.auth import auth as auth_module
from src.autobots.auth.auth import Auth

_USER = supabase_auth.User(
    id="00000000-0000-0000-0000-000000000000",
    aud="authenticated",
    app_metadata={},
    user_metadata={"full_name": "A B"},
    created_at="2026-01-01T00:00:00Z",
    email="a@b.co",
)
_SESSION_JSON = {
    "access_token": "acc",
    "refresh_token": "ref",
    "expires_in": 3600,
    "expires_at": 9999999999,
    "token_type": "bearer",
    "user": {
        "id": "00000000-0000-0000-0000-000000000000",
        "aud": "authenticated",
        "app_metadata": {},
        "user_metadata": {"full_name": "A B"},
        "created_at": "2026-01-01T00:00:00Z",
        "email": "a@b.co",
    },
}


def _mock_async_client(**methods) -> AsyncMock:
    client = AsyncMock()
    for name, value in methods.items():
        getattr(client, name).return_value = value
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    return client


@pytest.mark.asyncio
async def test_sign_up_passes_metadata_and_redirect_and_returns_auth_response():
    client = _mock_async_client(post=MagicMock(status_code=200, json=lambda: _SESSION_JSON))

    with patch.object(auth_module.httpx, "AsyncClient", return_value=client):
        res = await Auth().sign_up_with_password(
            "a@b.co", "pw", "http://x/cb", {"name": "A B", "full_name": "A B"}
        )

    url = client.post.call_args.args[0]
    kwargs = client.post.call_args.kwargs
    assert url.endswith("/auth/v1/signup")
    assert kwargs["json"]["data"] == {"name": "A B", "full_name": "A B"}
    assert kwargs["params"]["redirect_to"] == "http://x/cb"
    assert res.user.id == _USER.id
    assert res.session.access_token == "acc"


@pytest.mark.asyncio
async def test_sign_up_backward_compatible_without_options():
    client = _mock_async_client(post=MagicMock(status_code=200, json=lambda: _SESSION_JSON))

    with patch.object(auth_module.httpx, "AsyncClient", return_value=client):
        await Auth().sign_up_with_password("a@b.co", "pw")

    kwargs = client.post.call_args.kwargs
    assert kwargs.get("params") is None  # no redirect_to sent
    assert "data" not in kwargs["json"]  # no user_metadata


@pytest.mark.asyncio
async def test_verify_otp_delegates_and_returns_auth_response():
    client = _mock_async_client(post=MagicMock(status_code=200, json=lambda: _SESSION_JSON))

    with patch.object(auth_module.httpx, "AsyncClient", return_value=client):
        res = await Auth().verify_otp({"token_hash": "th", "type": "email"})

    assert client.post.call_args.args[0].endswith("/auth/v1/verify")
    assert client.post.call_args.kwargs["json"] == {"token_hash": "th", "type": "email"}
    assert res.session.access_token == "acc"


@pytest.mark.asyncio
async def test_verify_otp_error_raises_401():
    client = _mock_async_client(
        post=MagicMock(status_code=403, text="bad otp", json=lambda: {"msg": "bad otp"})
    )

    with patch.object(auth_module.httpx, "AsyncClient", return_value=client):
        with pytest.raises(HTTPException) as exc:
            await Auth().verify_otp({"token_hash": "th", "type": "email"})
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_resend_returns_auth_otp_response():
    client = _mock_async_client(
        post=MagicMock(status_code=200, content=b"x", json=lambda: {"message_id": "mid"})
    )

    with patch.object(auth_module.httpx, "AsyncClient", return_value=client):
        res = await Auth().resend({"type": "signup", "email": "a@b.co"})
    assert res.message_id == "mid"


@pytest.mark.asyncio
async def test_sign_out_with_token_calls_gotrue_logout():
    a = Auth()
    http_res = MagicMock(status_code=204)
    client = _mock_async_client(post=http_res)

    with patch.object(auth_module.httpx, "AsyncClient", return_value=client):
        await a.sign_out_with_token("tok")

    url = client.post.call_args.args[0]
    headers = client.post.call_args.kwargs["headers"]
    assert url.endswith("/auth/v1/logout")
    assert headers["Authorization"] == "Bearer tok"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code,should_raise", [(204, False), (401, False), (500, True)])
async def test_sign_out_with_token_tolerates_401_raises_on_5xx(status_code, should_raise):
    a = Auth()
    client = _mock_async_client(post=MagicMock(status_code=status_code, text="x"))

    with patch.object(auth_module.httpx, "AsyncClient", return_value=client):
        if should_raise:
            with pytest.raises(HTTPException):
                await a.sign_out_with_token("tok")
        else:
            await a.sign_out_with_token("tok")


@pytest.mark.asyncio
async def test_update_user_with_token_returns_user_response():
    a = Auth()
    user_json = {
        "id": str(_USER.id),
        "aud": "authenticated",
        "app_metadata": {},
        "user_metadata": {"full_name": "New"},
        "created_at": "2026-01-01T00:00:00Z",
        "email": "a@b.co",
    }
    client = _mock_async_client(put=MagicMock(status_code=200, json=lambda: user_json))

    with patch.object(auth_module.httpx, "AsyncClient", return_value=client):
        res = await a.update_user_with_token("tok", {"data": {"full_name": "New"}})

    assert client.put.call_args.args[0].endswith("/auth/v1/user")
    assert client.put.call_args.kwargs["json"] == {"data": {"full_name": "New"}}
    assert res.user.user_metadata["full_name"] == "New"


@pytest.mark.asyncio
async def test_update_user_with_token_propagates_error_status():
    a = Auth()
    client = _mock_async_client(put=MagicMock(status_code=422, text="weak password"))

    with patch.object(auth_module.httpx, "AsyncClient", return_value=client):
        with pytest.raises(HTTPException) as exc:
            await a.update_user_with_token("tok", {"password": "x"})
    assert exc.value.status_code == 422
