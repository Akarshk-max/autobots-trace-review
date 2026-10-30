"""Isolated unit tests for the auth provider abstraction (SupabaseAuth + registry).

These deliberately avoid the repo's live-DB / crypto session setup: the module-scoped ``start``
fixture below shadows the one in ``tests/conftest.py`` with a no-op, so the tests run without a
database or encryption keys. Token decoding is exercised against a throwaway ES256 keypair; user
provisioning and GoTrue are mocked.
"""
import base64
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import supabase_auth
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from jose import jwt

from src.autobots.auth.providers import supabase_auth as supabase_auth_mod
from src.autobots.auth.providers.auth_provider_registry import (
    AUTH_PROVIDER_MAP,
    DEFAULT_AUTH_PROVIDER,
    AuthProvider,
    get_auth_provider,
)
from src.autobots.auth.providers.supabase_auth import SupabaseAuth
from src.autobots.core.settings import SettingsProvider


@pytest.fixture(scope="session", autouse=True)
def start():
    """Override the repo's live-DB/crypto session setup — these tests need neither."""
    yield


# --- throwaway ES256 keypair (the code verifies against settings.JWT_PUBLIC_KEY) ---------
_PRIV = ec.generate_private_key(ec.SECP256R1())
_PRIV_PEM = _PRIV.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
_PUB_PEM = _PRIV.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
).decode()

NOW = int(time.time())

# "lean" token: only the claims the offline user path needs (sub/email/aud/iat).
LEAN = {
    "sub": "test-user-id",
    "email": "test@example.com",
    "aud": "authenticated",
    "role": "authenticated",
    "app_metadata": {"provider": "email", "providers": ["email"]},
    "user_metadata": {"full_name": "Test User"},
    "iat": NOW,
    "exp": NOW + 3600,
}
# "full" token: adds the strict claims required by the JwtPayload schema (aal/amr/session_id).
FULL = {
    **LEAN,
    "phone": "",
    "aal": "aal1",
    "amr": [{"method": "password", "timestamp": NOW}],
    "session_id": "test-session-id",
}


def mint(claims: dict, key: str = _PRIV_PEM) -> str:
    return jwt.encode(claims, key, algorithm="ES256")


@pytest.fixture(autouse=True)
def _point_settings_at_test_key(monkeypatch):
    """Point the JWT public key at our throwaway keypair for the duration of each test."""
    settings = SettingsProvider.sget()
    monkeypatch.setattr(settings, "JWT_ALGORITHM", "ES256")
    monkeypatch.setattr(
        settings, "JWT_PUBLIC_KEY_BASE64_ENCODED", base64.b64encode(_PUB_PEM.encode()).decode()
    )
    yield


# --------------------------------------------------------------------------------------------
# user_from_claims (pure)
# --------------------------------------------------------------------------------------------

def test_user_from_claims_maps_sub_email_created_at():
    ur = SupabaseAuth().user_from_claims(LEAN)
    assert isinstance(ur, supabase_auth.UserResponse)
    assert ur.user.id == "test-user-id"
    assert ur.user.email == "test@example.com"
    assert ur.user.aud == "authenticated"
    assert int(ur.user.created_at.timestamp()) == NOW


def test_user_from_claims_defaults_created_at_when_no_iat():
    claims = {k: v for k, v in LEAN.items() if k != "iat"}
    ur = SupabaseAuth().user_from_claims(claims)
    assert ur.user.created_at is not None


def test_user_from_claims_tolerates_missing_optional_claims():
    ur = SupabaseAuth().user_from_claims({"sub": "u1", "email": "a@b.co"})
    assert ur.user.id == "u1"
    assert ur.user.email == "a@b.co"
    assert ur.user.app_metadata == {} and ur.user.user_metadata == {}


# --------------------------------------------------------------------------------------------
# decode_claims_offline (real ES256 verify)
# --------------------------------------------------------------------------------------------

def test_decode_claims_offline_valid():
    claims = SupabaseAuth().decode_claims_offline(mint(LEAN))
    assert claims is not None and claims["sub"] == "test-user-id"


def test_decode_claims_offline_expired_rejected():
    assert SupabaseAuth().decode_claims_offline(mint({**LEAN, "exp": NOW - 10})) is None


def test_decode_claims_offline_expired_allowed_when_verify_exp_false():
    claims = SupabaseAuth().decode_claims_offline(mint({**LEAN, "exp": NOW - 10}), verify_exp=False)
    assert claims is not None and claims["sub"] == "test-user-id"


def test_decode_claims_offline_wrong_audience_rejected():
    assert SupabaseAuth().decode_claims_offline(mint({**LEAN, "aud": "other"})) is None


def test_decode_claims_offline_bad_signature_rejected():
    other = ec.generate_private_key(ec.SECP256R1())
    other_pem = other.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    assert SupabaseAuth().decode_claims_offline(mint(LEAN, key=other_pem)) is None


def test_decode_claims_offline_garbage_rejected():
    assert SupabaseAuth().decode_claims_offline("not.a.jwt") is None


# --------------------------------------------------------------------------------------------
# JwtPayload helpers (strict Supabase schema)
# --------------------------------------------------------------------------------------------

def test_decode_payload_offline_full_token():
    assert SupabaseAuth().decode_payload_offline(mint(FULL)) is not None


def test_decode_payload_offline_expired_none():
    assert SupabaseAuth().decode_payload_offline(mint({**FULL, "exp": NOW - 10})) is None


def test_decode_payload_allow_expired_returns_payload():
    assert SupabaseAuth().decode_payload_allow_expired(mint({**FULL, "exp": NOW - 10})) is not None


def test_decode_payload_offline_rejects_lean_missing_strict_claims():
    # LEAN omits aal/amr/session_id, which JwtPayload requires -> None (strict).
    assert SupabaseAuth().decode_payload_offline(mint(LEAN)) is None


# --------------------------------------------------------------------------------------------
# verify_token_offline / verify_token_online (provisioning + GoTrue mocked)
# --------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verify_token_offline_provisions_and_returns_user(monkeypatch):
    provision = AsyncMock()
    monkeypatch.setattr(supabase_auth_mod, "get_mongo_db", lambda: iter([object()]))
    monkeypatch.setattr(supabase_auth_mod.User, "create_if_not_exist", provision)

    ur = await SupabaseAuth().verify_token_offline(mint(LEAN))

    assert ur.user.id == "test-user-id"
    assert ur.user.email == "test@example.com"
    provision.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_token_offline_rejects_invalid_without_provisioning(monkeypatch):
    provision = AsyncMock()
    monkeypatch.setattr(supabase_auth_mod, "get_mongo_db", lambda: iter([object()]))
    monkeypatch.setattr(supabase_auth_mod.User, "create_if_not_exist", provision)

    with pytest.raises(HTTPException) as exc:
        await SupabaseAuth().verify_token_offline("garbage")

    assert exc.value.status_code == 403
    provision.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_token_online_calls_gotrue_and_provisions(monkeypatch):
    online_user = supabase_auth.UserResponse(
        user=supabase_auth.User(
            id="online-uid", email="online@example.com", app_metadata={}, user_metadata={},
            aud="authenticated", role="authenticated", created_at="2024-01-01T00:00:00Z",
        )
    )
    get_user = AsyncMock(return_value=online_user)
    provision = AsyncMock()
    monkeypatch.setattr(supabase_auth_mod, "get_auth", Mock(return_value=SimpleNamespace(get_user=get_user)))
    monkeypatch.setattr(supabase_auth_mod, "get_mongo_db", lambda: iter([object()]))
    monkeypatch.setattr(supabase_auth_mod.User, "create_if_not_exist", provision)

    ur = await SupabaseAuth().verify_token_online("some-token")

    assert ur is online_user
    get_user.assert_awaited_once_with("some-token")
    provision.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_token_online_rejects_user_without_email(monkeypatch):
    no_email = supabase_auth.UserResponse(
        user=supabase_auth.User(
            id="uid", email=None, app_metadata={}, user_metadata={}, aud="authenticated",
            created_at="2024-01-01T00:00:00Z",
        )
    )
    provision = AsyncMock()
    monkeypatch.setattr(supabase_auth_mod, "get_auth", Mock(return_value=SimpleNamespace(get_user=AsyncMock(return_value=no_email))))
    monkeypatch.setattr(supabase_auth_mod.User, "create_if_not_exist", provision)

    with pytest.raises(HTTPException) as exc:
        await SupabaseAuth().verify_token_online("some-token")

    assert exc.value.status_code == 401
    provision.assert_not_awaited()


# --------------------------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------------------------

def test_get_auth_provider_returns_supabase():
    assert isinstance(get_auth_provider(), SupabaseAuth)


def test_get_auth_provider_is_cached_singleton():
    assert get_auth_provider() is get_auth_provider()


def test_default_provider_and_registry_wiring():
    assert DEFAULT_AUTH_PROVIDER is AuthProvider.supabase
    assert AUTH_PROVIDER_MAP[AuthProvider.supabase] is SupabaseAuth


def test_get_auth_provider_unknown_raises():
    # A provider value absent from AUTH_PROVIDER_MAP raises rather than returning None.
    with pytest.raises(ValueError):
        get_auth_provider("not-a-provider")
