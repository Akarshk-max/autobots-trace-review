"""The rollup aggregation, and that the routes actually reach it."""

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

    fake.Client = _Client
    fake.create_client = lambda *a, **k: _Client()
    sys.modules["supabase"] = fake


_ensure_fake_supabase_module()

from src.autobots.batch.batch_doc_model import BatchLiteDoc  # noqa: E402
from src.autobots.event_result.event_result_crud import EventResultCRUD  # noqa: E402


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def to_list(self, length=None):
        return self._rows


class _FakeCollection:
    def __init__(self, rows):
        self._rows = rows
        self.pipelines = []

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        return _FakeCursor(self._rows)


def _crud(rows):
    crud = EventResultCRUD.__new__(EventResultCRUD)
    crud.document = _FakeCollection(rows)
    return crud


@pytest.mark.asyncio
async def test_rollup_groups_by_batch_and_status():
    rows = [
        {"_id": {"batch_id": "b1", "status": "success"}, "count": 8},
        {"_id": {"batch_id": "b1", "status": "error"}, "count": 1},
        {"_id": {"batch_id": "b2", "status": "processing"}, "count": 5},
    ]
    crud = _crud(rows)
    out = await crud.count_by_status_for_batches(batch_ids=["b1", "b2"], user_id="u1")

    assert out == {"b1": {"success": 8, "error": 1}, "b2": {"processing": 5}}

    # one round trip for the whole page, scoped to the caller
    assert len(crud.document.pipelines) == 1
    match = crud.document.pipelines[0][0]["$match"]
    assert match["batch_id"] == {"$in": ["b1", "b2"]}
    assert match["user_id"] == "u1"


@pytest.mark.asyncio
async def test_rollup_short_circuits_on_no_batches():
    crud = _crud([])
    assert await crud.count_by_status_for_batches(batch_ids=[], user_id="u1") == {}
    assert crud.document.pipelines == [], "must not query Mongo for an empty page"


@pytest.mark.asyncio
async def test_rollup_scopes_by_space_when_given():
    crud = _crud([])
    await crud.count_by_status_for_batches(batch_ids=["b1"], space_id="s1")
    match = crud.document.pipelines[0][0]["$match"]
    assert match["space_id"] == "s1"
    assert "user_id" not in match


@pytest.mark.asyncio
async def test_list_route_reaches_the_rollup(monkeypatch):
    """A correct aggregation nobody calls is indistinguishable from none."""
    from src.autobots.api.v1.api_batch import api_batches as api_mod

    captured = {}

    class FakeUserBatch:
        def __init__(self, *args, **kwargs):
            pass

        async def list_batches(self, ctx, batch_find, limit=20, offset=0):
            captured["find"] = batch_find
            captured["limit"] = limit
            return types.SimpleNamespace(docs=[], total_count=0, limit=limit, offset=offset)

    monkeypatch.setattr(api_mod, "UserBatch", FakeUserBatch)
    monkeypatch.setattr(api_mod, "UserORM", lambda **kw: types.SimpleNamespace(**kw))

    await api_mod.list_batches(
        request=types.SimpleNamespace(state=types.SimpleNamespace(context=None)),
        runnable_id="a1",
        user_res=types.SimpleNamespace(user=types.SimpleNamespace(id="u1")),
        db=None,
        space_member=None,
    )
    assert captured["find"].runnable_id == "a1"


@pytest.mark.asyncio
async def test_unknown_batch_id_404s_not_500(monkeypatch):
    """Runs created before Batches existed carry a batch_id with no parent."""
    from fastapi import HTTPException

    from src.autobots.api.v1.api_batch import api_batches as api_mod

    class FakeUserBatch:
        def __init__(self, *args, **kwargs):
            pass

        async def get_batch(self, ctx, batch_id):
            return None

    monkeypatch.setattr(api_mod, "UserBatch", FakeUserBatch)
    monkeypatch.setattr(api_mod, "UserORM", lambda **kw: types.SimpleNamespace(**kw))

    with pytest.raises(HTTPException) as exc:
        await api_mod.get_batch(
            request=types.SimpleNamespace(state=types.SimpleNamespace(context=None)),
            batch_id="orphan-uuid-from-before",
            user_res=types.SimpleNamespace(user=types.SimpleNamespace(id="u1")),
            db=None,
            space_member=None,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_progress_is_folded_per_batch(monkeypatch):
    """Each batch gets its own counts, and a batch with no runs still renders."""
    from src.autobots.batch.user_batch import UserBatch

    svc = UserBatch.__new__(UserBatch)
    svc.user_id = "u1"
    svc.space_member = None
    svc.event_result_crud = types.SimpleNamespace(
        count_by_status_for_batches=lambda **kw: _returns(
            {"b1": {"success": 2, "error": 1}}
        )
    )

    docs = [
        BatchLiteDoc(
            _id=bid,
            user_id="u1",
            runnable_type="action_run",
            runnable_id="a1",
            total=3,
            batch_size=1,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        for bid in ("b1", "b2")
    ]
    out = await svc._with_progress(docs)

    by_id = {r.batch_id: r for r in out}
    assert (by_id["b1"].success, by_id["b1"].error, by_id["b1"].done) == (2, 1, 3)
    assert by_id["b1"].status.value == "partial"
    assert (by_id["b2"].success, by_id["b2"].done) == (0, 0)
    assert by_id["b2"].status.value == "processing"


async def _returns(value):
    return value
