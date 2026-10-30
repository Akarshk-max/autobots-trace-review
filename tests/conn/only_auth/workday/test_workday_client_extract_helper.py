import importlib.util
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest


def _load_module(name: str, path: Path, monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_workday_class_exposes_extract_host_root_and_tenant_helper(monkeypatch: pytest.MonkeyPatch):
    clock_mod = types.ModuleType('src.autobots.core.clock.clock')
    clock_mod.Clock = type('Clock', (), {'datetime_now': staticmethod(datetime.now)})
    monkeypatch.setitem(sys.modules, 'src.autobots.core.clock.clock', clock_mod)

    context_mod = types.ModuleType('src.autobots.data_model.context')
    context_mod.Context = type('Context', (), {})
    monkeypatch.setitem(sys.modules, 'src.autobots.data_model.context', context_mod)

    module = _load_module(
        'src.autobots.conn.only_auth.workday.workday_client',
        Path('src/autobots/conn/only_auth/workday/workday_client.py'),
        monkeypatch,
    )

    candidates = (
        None,
        'https://wd5-services1.myworkday.com/ccx/api/vitalenergy',
        'https://wd5-services1.myworkday.com/ccx/oauth2/vitalenergy/token',
    )
    expected = module._extract_host_root_and_tenant(*candidates)
    actual = module.Workday._extract_host_root_and_tenant(*candidates)
    assert actual == expected
    assert actual == ('https://wd5-services1.myworkday.com', 'vitalenergy')
