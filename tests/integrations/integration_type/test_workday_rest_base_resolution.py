import asyncio
import importlib.util
import sys
import types
from datetime import datetime
from enum import Enum
from pathlib import Path

import pytest


def _load_module(name: str, path: Path, monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_workday_rest_base_resolution_helpers_work_for_saved_jwt_creds(monkeypatch: pytest.MonkeyPatch):
    clock_mod = types.ModuleType('src.autobots.core.clock.clock')
    clock_mod.Clock = type('Clock', (), {'datetime_now': staticmethod(datetime.now)})
    monkeypatch.setitem(sys.modules, 'src.autobots.core.clock.clock', clock_mod)

    context_mod = types.ModuleType('src.autobots.data_model.context')
    context_mod.Context = type('Context', (), {})
    monkeypatch.setitem(sys.modules, 'src.autobots.data_model.context', context_mod)

    # Loaded for its sys.modules registration side effect, which workday_auth imports from.
    _load_module(
        'src.autobots.conn.only_auth.workday.workday_client',
        Path('src/autobots/conn/only_auth/workday/workday_client.py'),
        monkeypatch,
    )

    app_auth_model_mod = types.ModuleType('src.autobots.integrations.integration_model')
    app_auth_model_mod.OAuthModel = type('OAuthModel', (), {})
    # workday_auth builds a consent URL now, so it imports the connect shapes too.
    app_auth_model_mod.IntegrationConnectRequest = type('IntegrationConnectRequest', (), {})
    app_auth_model_mod.IntegrationConnectResolved = type('IntegrationConnectResolved', (), {})
    app_auth_model_mod.IntegrationConnectResult = type('IntegrationConnectResult', (), {})
    monkeypatch.setitem(sys.modules, 'src.autobots.integrations.integration_model', app_auth_model_mod)

    app_auth_type_mod = types.ModuleType('src.autobots.integrations.integration_type.integration_type')
    app_auth_type_mod.IntegrationTypeABC = type('IntegrationTypeABC', (), {})
    monkeypatch.setitem(sys.modules, 'src.autobots.integrations.integration_type.integration_type', app_auth_type_mod)

    class AppTypes(Enum):
        workday = 'workday'
    app_types_mod = types.ModuleType('src.autobots.integrations.app_types')
    app_types_mod.AppTypes = AppTypes
    monkeypatch.setitem(sys.modules, 'src.autobots.integrations.app_types', app_types_mod)

    workday_jwt_mod = types.ModuleType('src.autobots.conn.only_auth.workday.workday_jwt')
    async def connect_workday_jwt(ctx, payload, try_fallbacks=True):
        raise AssertionError('connect_workday_jwt should not be called in this test')
    workday_jwt_mod.connect_workday_jwt = connect_workday_jwt
    monkeypatch.setitem(sys.modules, 'src.autobots.conn.only_auth.workday.workday_jwt', workday_jwt_mod)

    module = _load_module(
        'src.autobots.integrations.integration_type.workday_auth',
        Path('src/autobots/integrations/integration_type/workday_auth.py'),
        monkeypatch,
    )

    creds = module.WorkdayCreds(
        auth_type='jwt_certificate',
        client_id='client',
        tenant_url='https://wd5-services1.myworkday.com/ccx/api/vitalenergy',
        token_url='https://wd5-services1.myworkday.com/ccx/oauth2/vitalenergy/token',
        scopes='Staffing',
        jwt_method='jwt_bearer',
        integration_user='OSC_Snapshot_Extract_ISU',
        key_id='dc5a42c4-c052-4594-a61a-2c4db37ab393',
        private_key_path='s3://bucket/private_key.pem',
    )

    assert module.WorkdayAuth._preferred_rest_base_url(creds) == 'https://wd5-services1.myworkday.com/ccx/api/v1/vitalenergy'
    candidates = module.WorkdayAuth._build_rest_base_candidates(creds)
    assert 'https://wd5-services1.myworkday.com/ccx/api/v1/vitalenergy' in candidates
    probe_urls = module.WorkdayAuth._build_rest_workers_probe_urls(creds)
    assert probe_urls[0].endswith('/workers?limit=1')
    assert module.WorkdayAuth._build_rest_workers_me_probe_urls(creds)[0].endswith('/workers/me')
    assert asyncio.run(module.WorkdayAuth.get_api_domain(creds)) == 'https://wd5-services1.myworkday.com/ccx/api/v1/vitalenergy'
