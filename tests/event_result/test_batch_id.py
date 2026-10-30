"""batch_id models and the list-endpoint filter (issue #130)."""

from __future__ import annotations

import sys
import types

import pytest


def _ensure_fake_supabase_module() -> None:
    """Routers import auth code that imports the supabase connector."""
    if "supabase" in sys.modules:
        return
    fake = types.ModuleType("supabase")

    class _Client:
        pass

    def create_client(*args, **kwargs):  # noqa: ANN001
        return _Client()

    fake.Client = _Client
    fake.create_client = create_client
    sys.modules["supabase"] = fake


_ensure_fake_supabase_module()

from src.autobots.event_result.event_result_model import (  # noqa: E402
    EventResultCreate,
    EventResultDocFind,
    EventResultFind,
    EventResultLiteDoc,
    EventResultUpdate,
)


def _fake_request():
    return types.SimpleNamespace(state=types.SimpleNamespace(context=None))


def _fake_user_res():
    return types.SimpleNamespace(user=types.SimpleNamespace(id="u1", model_dump=lambda: {"id": "u1"}))


def test_batch_id_declared_on_write_read_and_projection_models():
    assert "batch_id" in EventResultCreate.model_fields
    assert "batch_id" in EventResultFind.model_fields
    assert "batch_id" in EventResultDocFind.model_fields
    # EventResultLiteDoc is the projection in CRUDBase._find_raw
    assert "batch_id" in EventResultLiteDoc.model_fields


def test_batch_id_is_not_updatable():
    """Leaving it off the update model is what stops a rerun clobbering it."""
    assert "batch_id" not in EventResultUpdate.model_fields


def test_batch_id_defaults_to_none():
    assert EventResultFind().batch_id is None


@pytest.mark.asyncio
async def test_build_find_params_emits_batch_id():
    from src.autobots.core.database.mongo_base_crud import CRUDBase

    crud = CRUDBase.__new__(CRUDBase)  # no DB needed for the pure filter builder

    doc_find = EventResultDocFind(user_id="u1", batch_id="b-1")
    params = await crud._build_find_params(doc_find.model_dump(exclude_none=True), {})
    assert params["batch_id"] == "b-1"

    doc_find_without = EventResultDocFind(user_id="u1")
    params_without = await crud._build_find_params(
        doc_find_without.model_dump(exclude_none=True), {}
    )
    assert "batch_id" not in params_without


class _CapturingService:
    captured = None

    def __init__(self, *args, **kwargs):
        pass

    async def _capture(self, find, *args, **kwargs):
        type(self).captured = find
        return types.SimpleNamespace(docs=[], total_count=0, limit=20, offset=0)


@pytest.mark.asyncio
async def test_list_action_result_passes_batch_id_to_find(monkeypatch):
    from src.autobots.api.v1.api_action_result import api_action_results as api_mod

    class Fake(_CapturingService):
        async def list_owned_or_shared_action_results(self, find, limit=20, offset=0):
            return await self._capture(find)

    monkeypatch.setattr(api_mod, "UserActionResult", Fake)
    monkeypatch.setattr(api_mod, "UserORM", lambda **kwargs: types.SimpleNamespace(**kwargs))

    await api_mod.list_action_result(
        request=_fake_request(),
        batch_id="b-123",
        user_res=_fake_user_res(),
        db=None,
        space_member=None,
    )
    assert Fake.captured.batch_id == "b-123"


@pytest.mark.asyncio
async def test_list_action_result_batch_id_defaults_to_none(monkeypatch):
    from src.autobots.api.v1.api_action_result import api_action_results as api_mod

    class Fake(_CapturingService):
        async def list_owned_or_shared_action_results(self, find, limit=20, offset=0):
            return await self._capture(find)

    monkeypatch.setattr(api_mod, "UserActionResult", Fake)
    monkeypatch.setattr(api_mod, "UserORM", lambda **kwargs: types.SimpleNamespace(**kwargs))

    await api_mod.list_action_result(
        request=_fake_request(),
        user_res=_fake_user_res(),
        db=None,
        space_member=None,
    )
    assert Fake.captured.batch_id is None


@pytest.mark.asyncio
async def test_list_action_graph_result_passes_batch_id_to_find(monkeypatch):
    from src.autobots.api.v1.api_action_graph_result import api_action_graph_results as api_mod

    class Fake(_CapturingService):
        async def list_page_owned_or_shared_action_graph_result(self, find, limit=20, offset=0):
            return await self._capture(find)

    monkeypatch.setattr(api_mod, "UserActionGraphResult", Fake)
    monkeypatch.setattr(api_mod, "UserORM", lambda **kwargs: types.SimpleNamespace(**kwargs))

    await api_mod.list_action_graph_result(
        request=_fake_request(),
        batch_id="b-456",
        user_res=_fake_user_res(),
        db=None,
        space_member=None,
    )
    assert Fake.captured.batch_id == "b-456"


@pytest.mark.asyncio
async def test_list_action_team_results_passes_batch_id_to_find(monkeypatch):
    from src.autobots.api.v1.api_action_team import api_action_team_results as api_mod

    class Fake(_CapturingService):
        async def list_page_owned_or_shared_action_team_result(self, find, limit=20, offset=0):
            return await self._capture(find)

    monkeypatch.setattr(api_mod, "UserActionTeamResult", Fake)
    monkeypatch.setattr(api_mod, "UserORM", lambda **kwargs: types.SimpleNamespace(**kwargs))

    await api_mod.list_action_team_results(
        request=_fake_request(),
        batch_id="b-789",
        user_res=_fake_user_res(),
        db=None,
        space_member=None,
    )
    assert Fake.captured.batch_id == "b-789"


@pytest.mark.asyncio
async def test_space_admin_branch_also_receives_batch_id(monkeypatch):
    """The space-admin path takes a different service class."""
    from src.autobots.api.v1.api_action_result import api_action_results as api_mod

    class FakeSpace(_CapturingService):
        async def list_action_results(self, ctx, find, limit=20, offset=0):
            return await self._capture(find)

    monkeypatch.setattr(api_mod, "SpaceActionResults", FakeSpace)
    monkeypatch.setattr(api_mod, "is_space_member_a_admin", lambda space_member: True)

    await api_mod.list_action_result(
        request=_fake_request(),
        batch_id="b-space",
        user_res=_fake_user_res(),
        db=None,
        space_member=types.SimpleNamespace(space_id="s1"),
    )
    assert FakeSpace.captured.batch_id == "b-space"
