import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import jwt

from src.autobots.integrations.integration_type.workday_auth import WorkdayCreds
from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_types import AppTypes
from src.autobots.conn.only_auth.workday.workday_jwt import (
    _normalize_scope,
    _suggestions_for_response,
    build_signed_jwt,
    resolve_token_url,
)


def make_private_key_pem() -> str:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def test_resolve_token_url_from_api_base():
    token_url, tenant, warnings = resolve_token_url(
        'https://wd5-services1.myworkday.com/ccx/api/vitalenergy', None
    )
    assert token_url == 'https://wd5-services1.myworkday.com/ccx/oauth2/vitalenergy/token'
    assert tenant == 'vitalenergy'
    assert warnings == []


def test_resolve_token_url_from_login_url_flags_warning():
    token_url, tenant, warnings = resolve_token_url(
        'https://wd5-identity.myworkday.com/wday/authgwy/vitalenergy/upc/login?redirect=n', None
    )
    assert token_url == 'https://wd5-services1.myworkday.com/ccx/oauth2/vitalenergy/token'
    assert tenant == 'vitalenergy'
    assert any('login URL' in w for w in warnings)


def test_normalize_scope():
    assert _normalize_scope('System, Integration\nStaffing') == 'System Integration Staffing'


def test_suggestions_for_invalid_request():
    suggestions = _suggestions_for_response(400, '{"error":"Invalid request"}')
    assert any('endpoint exists' in s.lower() for s in suggestions)


def test_build_signed_jwt_claims():
    pem = make_private_key_pem()
    token, claims, headers = build_signed_jwt(
        'client-id',
        'https://wd5-services1.myworkday.com/ccx/oauth2/vitalenergy/token',
        pem,
        'client-id',
        'kid-1',
    )
    parsed_claims = jwt.get_unverified_claims(token)
    parsed_headers = jwt.get_unverified_header(token)
    assert parsed_claims['iss'] == 'client-id'
    assert parsed_claims['sub'] == 'client-id'
    assert parsed_claims['aud'].endswith('/ccx/oauth2/vitalenergy/token')
    assert parsed_headers['kid'] == 'kid-1'
    assert claims['iss'] == parsed_claims['iss']
    assert headers['kid'] == parsed_headers['kid']


def test_workday_creds_accepts_jwt_payload_shape():
    creds = WorkdayCreds.model_validate({
        'client_id': 'abc',
        'tenant_url': 'https://wd5-services1.myworkday.com/ccx/api/vitalenergy',
        'auth_type': 'jwt_certificate',
        'private_key_path': 's3://bucket/key.pem',
        'token_url': 'https://wd5-services1.myworkday.com/ccx/oauth2/vitalenergy/token',
        'scopes': 'System Integration',
        'jwt_method': 'client_assertion',
    })
    assert creds.auth_type == 'jwt_certificate'
    assert creds.private_key_path.startswith('s3://')


def test_validate_creds_for_app_returns_http_400_for_invalid_payload():
    with pytest.raises(HTTPException) as exc:
        UserAppCreds._validate_creds_for_app(
            AppTypes.workday,
            {
                'client_id': 'abc',
                'tenant_url': 'https://wd5-services1.myworkday.com/ccx/api/vitalenergy',
                'auth_type': 'jwt_certificate',
                # missing private_key_path
            },
            phase='update',
        )
    assert exc.value.status_code == 400
    assert exc.value.detail['phase'] == 'update'


from types import SimpleNamespace

from src.autobots.integrations.integration_type.workday_auth import WorkdayAuth


@pytest.mark.asyncio
async def test_workday_refresh_auth_uses_auto_negotiated_jwt(monkeypatch):
    async def fake_connect_workday_jwt(ctx, payload, try_fallbacks=True):
        # refresh_auth reads the token out of token_response and logs jwt_result.method,
        # so the fake has to match WorkdayConnectResult's real shape.
        return SimpleNamespace(
            method="jwt_bearer",
            subject="OSC_Snapshot_Extract_ISU",
            token_response={"access_token": "token-123", "expires_in": 3600},
        )

    monkeypatch.setattr(
        "src.autobots.integrations.integration_type.workday_auth.connect_workday_jwt",
        fake_connect_workday_jwt,
    )

    creds = WorkdayCreds(
        auth_type="jwt_certificate",
        client_id="cid",
        tenant_url="https://wd5-services1.myworkday.com/ccx/api/vitalenergy",
        private_key_path="s3://bucket/key.pem",
    )

    result = await WorkdayAuth.refresh_auth(ctx=None, app_creds=creds, app_auth=None)
    assert not isinstance(result, Exception)
    assert result.access_token == "token-123"
    assert result.expires_in == 3600
