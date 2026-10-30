
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from jose import jwt as jose_jwt

from src.autobots.api.v1.api_auth import api_workday_auth
from src.autobots.integrations.integration_type.workday_auth import WorkdayCreds
from src.autobots.conn.only_auth.workday.workday_jwt import (
    WorkdayConnectResult,
    WorkdayDebugResult,
)
from src.autobots.data_model.context import Context


@pytest.mark.asyncio
async def test_workday_oauth_url_uses_saved_creds_helper_for_jwt(monkeypatch):
    calls = {}

    async def fake_get_saved_workday_creds(ctx, db, user, name):
        calls['ctx_user_id'] = getattr(ctx, 'user_id', None)
        calls['db'] = db
        calls['user_id'] = getattr(user, 'id', None)
        return WorkdayCreds(
            auth_type='jwt_certificate',
            client_id='cid',
            tenant_url='https://wd5-services1.myworkday.com/ccx/api/vitalenergy',
            private_key_path='s3://bucket/key.pem',
        )

    async def fake_workday_connect_saved(request, name, user_res, db):
        calls['connect_called'] = True
        calls['connect_db'] = db
        calls['connect_user_id'] = getattr(user_res.user, 'id', None)
        return {'message': 'ok'}

    monkeypatch.setattr(api_workday_auth, '_get_saved_workday_creds', fake_get_saved_workday_creds)
    monkeypatch.setattr(api_workday_auth, 'workday_connect_saved', fake_workday_connect_saved)
    monkeypatch.setattr(api_workday_auth, '_post_connect_redirect_url', lambda request: 'https://dev.meetkiwi.ai/dashboard/connections?app=workday&connected=1')

    # The endpoint takes its context from the request, as the middleware supplies it.
    request = SimpleNamespace(headers={}, state=SimpleNamespace(context=Context()))
    user_res = SimpleNamespace(user=SimpleNamespace(id='user-123'))
    db = object()

    result = await api_workday_auth.workday_oauth_url(
        request=request,
        name='wd',
        redirect_uri='https://dev.meetkiwi.ai/api/workday-callback-app-link',
        user_res=user_res,
        db=db,
    )

    assert calls['ctx_user_id'] == 'user-123'
    assert calls['user_id'] == 'user-123'
    assert calls['db'] is db
    assert calls['connect_called'] is True
    assert calls['connect_db'] is db
    assert calls['connect_user_id'] == 'user-123'
    assert result['url'] == 'https://dev.meetkiwi.ai/dashboard/connections?app=workday&connected=1'


@pytest.mark.asyncio
async def test_workday_jwt_auth_merges_saved_creds_and_uses_jwt_route(monkeypatch):
    calls = {}

    async def fake_get_saved_workday_creds(ctx, db, user, name):
        return WorkdayCreds(
            auth_type='jwt_certificate',
            client_id='saved-cid',
            tenant_url='https://wd5-services1.myworkday.com/ccx/api/vitalenergy',
            token_url='https://wd5-services1.myworkday.com/ccx/oauth2/vitalenergy/token',
            jwt_method='jwt_bearer',
            integration_user='OSC_Snapshot_Extract_ISU',
            private_key_path='s3://bucket/key.pem',
        )

    async def fake_persist_workday_jwt_connect(ctx, db, user, name, payload, route_name):
        calls['payload'] = payload
        calls['route_name'] = route_name
        calls['user_id'] = getattr(user, 'id', None)
        return {'message': 'ok', 'subject': payload.get('integration_user')}

    monkeypatch.setattr(api_workday_auth, '_get_saved_workday_creds', fake_get_saved_workday_creds)
    monkeypatch.setattr(api_workday_auth, '_persist_workday_jwt_connect', fake_persist_workday_jwt_connect)

    request = SimpleNamespace(headers={})
    user_res = SimpleNamespace(user=SimpleNamespace(id='user-123'))
    db = object()
    debug_request = api_workday_auth.WorkdayDebugRequest(
        auth_type='jwt_certificate',
        client_id='incoming-cid',
        tenant_url='https://wd5-services1.myworkday.com/ccx/api/vitalenergy',
        token_url='https://wd5-services1.myworkday.com/ccx/oauth2/vitalenergy/token',
        jwt_method='jwt_bearer',
        key_id='kid',
        public_certificate='pem',
        private_key_path='s3://bucket/key.pem',
    )

    result = await api_workday_auth.workday_jwt_auth(
        request=request,
        name='wd',
        debug_request=debug_request,
        user_res=user_res,
        db=db,
    )

    assert calls['route_name'] == 'jwt-auth'
    assert calls['user_id'] == 'user-123'
    assert calls['payload']['client_id'] == 'incoming-cid'
    assert calls['payload']['jwt_method'] == 'jwt_bearer'
    assert calls['payload']['integration_user'] == 'OSC_Snapshot_Extract_ISU'
    assert result['subject'] == 'OSC_Snapshot_Extract_ISU'



def test_normalize_workday_token_response_infers_expires_in_from_jwt():
    exp = int((datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp())
    token = jose_jwt.encode({'exp': exp}, 'secret', algorithm='HS256')

    normalized, meta = api_workday_auth._normalize_workday_token_response(
        {'access_token': token, 'token_type': 'Bearer'}
    )

    assert normalized['token_type'] == 'Bearer'
    assert normalized['expires_in'] > 0
    assert normalized['expires_in'] <= 600
    assert meta['token_response_had_expires_in'] is False
    assert meta['token_response_inferred_expires_in'] is not None


def test_normalize_workday_token_response_defaults_when_exp_missing():
    normalized, meta = api_workday_auth._normalize_workday_token_response(
        {'access_token': 'opaque-token', 'token_type': 'Bearer'}
    )

    assert normalized['expires_in'] == 3600
    assert meta['token_response_had_expires_in'] is False
    assert meta['token_response_inferred_expires_in'] is None


@pytest.mark.asyncio
async def test_persist_workday_jwt_connect_normalizes_missing_expires_in(monkeypatch):
    exp = int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())
    token = jose_jwt.encode({'exp': exp}, 'secret', algorithm='HS256')

    async def fake_connect_workday_jwt(ctx, payload):
        return WorkdayConnectResult(
            success=True,
            method='jwt_bearer',
            subject='OSC_Snapshot_Extract_ISU',
            token_response={'access_token': token, 'token_type': 'Bearer'},
            debug=WorkdayDebugResult(resolved_token_url='https://example.com/token'),
        )

    async def fake_create(ctx, db, user, integration_create, decrypt=False):
        return SimpleNamespace(id='auth-123')

    monkeypatch.setattr(api_workday_auth, 'connect_workday_jwt', fake_connect_workday_jwt)
    monkeypatch.setattr(api_workday_auth.UserIntegration, 'create', fake_create)

    result = await api_workday_auth._persist_workday_jwt_connect(
        ctx=Context(user_id='user-123'),
        db=object(),
        user=SimpleNamespace(id='user-123'),
        name='wd',
        payload={
            'auth_type': 'jwt_certificate',
            'client_id': 'cid',
            'tenant_url': 'https://wd5-services1.myworkday.com/ccx/api/vitalenergy',
            'token_url': 'https://wd5-services1.myworkday.com/ccx/oauth2/vitalenergy/token',
            'jwt_method': 'jwt_bearer',
            'integration_user': 'OSC_Snapshot_Extract_ISU',
            'private_key_path': 's3://bucket/key.pem',
        },
        route_name='jwt-auth',
    )

    assert result['auth_id'] == 'auth-123'
    assert result['method'] == 'jwt_bearer'
    assert result['subject'] == 'OSC_Snapshot_Extract_ISU'
