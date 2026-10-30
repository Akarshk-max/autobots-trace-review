"""Each batch_run call records exactly one Batch, and its id is what the runs carry."""

from __future__ import annotations

import types

import pytest

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action_graph.action_graph.action_graph_doc_model import ActionGraphDoc
from src.autobots.action_team.action_team.action_team_doc_model import (
    ActionTeamDoc,
    TeamMember,
    TeamMemberRole,
)
from src.autobots.data_model.context import Context


def _action_doc() -> ActionDoc:
    return ActionDoc(
        _id="action-1",
        name="Summarise",
        type=ActionType.text2text_llm_chat_openai,
        user_id="u1",
        created_at="2026-01-01T00:00:00Z",
    )


def _graph_doc() -> ActionGraphDoc:
    return ActionGraphDoc(
        _id="graph-1", name="Summarise each", user_id="u1", created_at="2026-01-01T00:00:00Z"
    )


def _team_doc() -> ActionTeamDoc:
    return ActionTeamDoc(
        _id="team-1",
        name="Research team",
        user_id="u1",
        created_at="2026-01-01T00:00:00Z",
        members=[TeamMember(action_id="action-1", role=TeamMemberRole.leader)],
    )


class _CapturingBatchService:
    """Stands in for BatchService, recording every batch a call creates."""

    def __init__(self):
        self.created = []

    async def create_batch_for_run(self, **kwargs):
        self.created.append(kwargs)
        return types.SimpleNamespace(id=f"batch-{len(self.created)}")


class _FakeUserActionResult:
    def __init__(self):
        self.creates = []
        self.counter = 0

    async def create_action_result(self, action_result_create):
        self.creates.append(action_result_create)
        self.counter += 1
        return types.SimpleNamespace(id=f"result-{self.counter}")

    async def get_action_result(self, action_result_id):
        return types.SimpleNamespace(id=action_result_id, name="Existing run")

    async def init(self, action_result_id=None, **kwargs):
        # Rerun path: the real init updates the existing run, it never creates one.
        return await self.get_action_result(action_result_id)


@pytest.fixture
def action_runner(monkeypatch):
    from src.autobots.action.action import user_actions as ua_mod

    svc = _CapturingBatchService()

    async def fake_gen_title(*args, **kwargs):
        return "Generated title"

    async def fake_run_in_background(*args, **kwargs):
        return types.SimpleNamespace(id=kwargs.get("action_result_id"), status="processing")

    monkeypatch.setattr(ua_mod.LlmFunc, "gen_title", staticmethod(fake_gen_title))
    monkeypatch.setattr(
        ua_mod.ActionFactory, "run_action_in_background", staticmethod(fake_run_in_background)
    )
    monkeypatch.setattr(
        ua_mod.BatchService, "create_batch_for_run", svc.create_batch_for_run
    )
    return ua_mod, svc


async def _run_action_batch(ua_mod, results_service, action_input, **kwargs):
    user_actions = ua_mod.UserActions.__new__(ua_mod.UserActions)
    user_actions.space_member = None
    user_actions.db = None
    user_actions.user = types.SimpleNamespace(id="u1")
    return await user_actions.batch_run_action_in_background(
        ctx=Context(user_id="u1"),
        app_state=types.SimpleNamespace(),
        action_doc=_action_doc(),
        action_input=action_input,
        user_action_result=results_service,
        db=None,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_action_batch_records_one_batch_with_total(action_runner):
    ua_mod, svc = action_runner
    results = _FakeUserActionResult()
    await _run_action_batch(
        ua_mod,
        results,
        DataBlock(urls=["http://a.com", "http://b.com", "http://c.com"]),
        batch_size=1,
    )

    assert len(svc.created) == 1, "one batch_run call must record exactly one Batch"
    batch = svc.created[0]
    assert batch["total"] == 3, "total must match the fan-out width"
    assert batch["runnable_type"].value == "action_run"
    assert batch["runnable_id"] == "action-1"
    assert batch["batch_size"] == 1

    # every run points back at that batch
    assert len(results.creates) == 3
    assert {c.batch_id for c in results.creates} == {"batch-1"}


@pytest.mark.asyncio
async def test_action_no_urls_is_a_batch_of_one(action_runner):
    ua_mod, svc = action_runner
    results = _FakeUserActionResult()
    await _run_action_batch(ua_mod, results, DataBlock(text="just some text"))

    assert len(svc.created) == 1
    assert svc.created[0]["total"] == 1
    assert len(results.creates) == 1
    assert results.creates[0].batch_id == "batch-1"


@pytest.mark.asyncio
async def test_action_rerun_creates_no_batch(action_runner):
    """A rerun updates an existing run rather than creating any, so it is not a batch."""
    ua_mod, svc = action_runner
    results = _FakeUserActionResult()
    await _run_action_batch(
        ua_mod, results, DataBlock(text="just some text"), action_result_id="existing-1"
    )

    assert svc.created == []
    assert results.creates == []


@pytest.mark.asyncio
async def test_graph_batch_records_one_batch(monkeypatch):
    from src.autobots.action_graph.action_graph import user_action_graph as uag_mod

    svc = _CapturingBatchService()
    seen: list[str | None] = []

    async def fake_run_in_background(*args, **kwargs):
        seen.append(kwargs.get("batch_id"))
        return types.SimpleNamespace(id="r", status="processing")

    async def fake_gen_title(*args, **kwargs):
        return "Generated title"

    async def fake_get_owned_or_shared(self, ctx, action_graph_id):
        return _graph_doc()

    monkeypatch.setattr(
        uag_mod.ActionGraph, "run_in_background", staticmethod(fake_run_in_background)
    )
    monkeypatch.setattr(uag_mod.LlmFunc, "gen_title", staticmethod(fake_gen_title))
    monkeypatch.setattr(uag_mod.BatchService, "create_batch_for_run", svc.create_batch_for_run)
    monkeypatch.setattr(uag_mod.UserActionGraphs, "get_owned_or_shared", fake_get_owned_or_shared)

    user_graphs = uag_mod.UserActionGraphs.__new__(uag_mod.UserActionGraphs)
    user_graphs.space_member = None
    user_graphs.db = None
    user_graphs.user = types.SimpleNamespace(id="u1", email="u1@example.com")
    user_graphs.user_id = "u1"

    await user_graphs.batch_run_in_background(
        ctx=Context(user_id="u1"),
        app_state=types.SimpleNamespace(),
        user_actions=types.SimpleNamespace(db=None, user=types.SimpleNamespace(id="u1")),
        user_action_graph_result=types.SimpleNamespace(),
        action_graph_id="graph-1",
        input=DataBlock(urls=["http://a.com", "http://b.com", "http://c.com"]),
        batch_size=1,
        action_graph_doc=_graph_doc(),
    )

    assert len(svc.created) == 1
    assert svc.created[0]["total"] == 3
    assert svc.created[0]["runnable_type"].value == "action_graph_run"
    assert set(seen) == {"batch-1"}


@pytest.mark.asyncio
async def test_team_batch_records_one_batch(monkeypatch):
    from src.autobots.action_team.action_team import user_action_team as uat_mod

    svc = _CapturingBatchService()
    seen: list[str | None] = []

    async def fake_run_in_background(*args, **kwargs):
        seen.append(kwargs.get("batch_id"))
        return types.SimpleNamespace(id="r", status="processing")

    monkeypatch.setattr(
        uat_mod.ActionTeam, "run_in_background", staticmethod(fake_run_in_background)
    )
    monkeypatch.setattr(uat_mod.BatchService, "create_batch_for_run", svc.create_batch_for_run)

    user_teams = uat_mod.UserActionTeams.__new__(uat_mod.UserActionTeams)
    user_teams.space_member = None
    user_teams.db = None
    user_teams.user = types.SimpleNamespace(id="u1")

    await user_teams.batch_run_in_background(
        ctx=Context(user_id="u1"),
        app_state=types.SimpleNamespace(),
        user_actions=types.SimpleNamespace(db=None, user=types.SimpleNamespace(id="u1")),
        user_action_team_result=types.SimpleNamespace(),
        action_team_id="team-1",
        input=DataBlock(urls=["http://a.com", "http://b.com"]),
        batch_size=1,
        action_team_doc=_team_doc(),
    )

    assert len(svc.created) == 1
    assert svc.created[0]["total"] == 2
    assert svc.created[0]["runnable_type"].value == "action_team_run"
    # teams have no batch-level title, so the team's own name stands in
    assert svc.created[0]["name"] == "Research team"
    assert set(seen) == {"batch-1"}
