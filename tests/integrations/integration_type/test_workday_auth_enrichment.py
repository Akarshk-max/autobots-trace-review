from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from jose import jwt as jose_jwt

from src.autobots.integrations.integration_type.workday_auth import WorkdayAuth, WorkdayCreds
from src.autobots.conn.only_auth.workday.workday_client import Workday
from src.autobots.conn.only_auth.workday.workday_jwt import WorkdayConnectResult, WorkdayDebugResult
from src.autobots.data_model.context import Context


def test_workday_client_derives_token_url_from_tenant_api_base():
    url = Workday.get_token_url('https://wd5-services1.myworkday.com/ccx/api/vitalenergy')
    assert url == 'https://wd5-services1.myworkday.com/ccx/oauth2/vitalenergy/token'


def test_workday_client_uses_explicit_token_url_for_authorize_url():
    url = Workday.get_authorize_url(
        'https://wd5-services1.myworkday.com/ccx/api/vitalenergy',
        token_url='https://wd5-services1.myworkday.com/ccx/oauth2/vitalenergy/token',
    )
    assert url == 'https://wd5-services1.myworkday.com/ccx/oauth2/vitalenergy/authorize'


@pytest.mark.asyncio
async def test_workday_get_updated_url_rewrites_generic_workers_me_url():
    creds = WorkdayCreds(
        auth_type='jwt_certificate',
        client_id='cid',
        tenant_url='https://wd5-services1.myworkday.com/ccx/api/vitalenergy',
        token_url='https://wd5-services1.myworkday.com/ccx/oauth2/vitalenergy/token',
        private_key_path='s3://bucket/key.pem',
        integration_user='OSC_Snapshot_Extract_ISU',
    )
    updated = await WorkdayAuth.get_updated_url(
        ctx=Context(user_id='user-123'),
        app_creds=creds,
        app_auth=SimpleNamespace(access_token='token'),
        url='https://api.workday.com/ccx/api/v1/workers/me',
    )
    assert updated == 'https://wd5-services1.myworkday.com/ccx/api/v1/vitalenergy/workers/me'


@pytest.mark.asyncio
async def test_workday_refresh_auth_jwt_normalizes_token_response(monkeypatch):
    exp = int((datetime.now(timezone.utc) + timedelta(minutes=15)).timestamp())
    token = jose_jwt.encode({'exp': exp}, 'secret', algorithm='HS256')

    async def fake_connect_workday_jwt(ctx, payload, try_fallbacks=True):
        return WorkdayConnectResult(
            success=True,
            method='jwt_bearer',
            subject='OSC_Snapshot_Extract_ISU',
            token_response={'access_token': token, 'token_type': 'Bearer'},
            debug=WorkdayDebugResult(resolved_token_url='https://example.com/token'),
        )

    monkeypatch.setattr('src.autobots.integrations.integration_type.workday_auth.connect_workday_jwt', fake_connect_workday_jwt)

    creds = WorkdayCreds(
        auth_type='jwt_certificate',
        client_id='cid',
        tenant_url='https://wd5-services1.myworkday.com/ccx/api/vitalenergy',
        token_url='https://wd5-services1.myworkday.com/ccx/oauth2/vitalenergy/token',
        private_key_path='s3://bucket/key.pem',
        integration_user='OSC_Snapshot_Extract_ISU',
    )
    refreshed = await WorkdayAuth.refresh_auth(Context(user_id='user-123'), creds, None)
    assert refreshed.access_token == token
    assert refreshed.expires_in > 0
