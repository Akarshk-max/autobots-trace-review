"""batch_id survives the graph/team thread-through: runner -> init -> *ResultCreate (issue #130)."""

from __future__ import annotations

import types

import pytest

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action_graph.action_graph.action_graph_doc_model import ActionGraphDoc
from src.autobots.action_team.action_team.action_team_doc_model import (
    ActionTeamDoc,
    TeamMember,
    TeamMemberRole,
)


def _graph_doc() -> ActionGraphDoc:
    return ActionGraphDoc(
        _id="graph-1",
        name="Summarise each",
        user_id="u1",
        created_at="2026-01-01T00:00:00Z",
    )


def _team_doc() -> ActionTeamDoc:
    return ActionTeamDoc(
        _id="team-1",
        name="Research team",
        user_id="u1",
        created_at="2026-01-01T00:00:00Z",
        members=[TeamMember(action_id="action-1", role=TeamMemberRole.leader)],
    )


@pytest.mark.asyncio
async def test_graph_batch_run_forwards_one_shared_batch_id(monkeypatch):
    from src.autobots.action_graph.action_graph import user_action_graph as uag_mod

    seen: list[str | None] = []

    async def fake_run_in_background(*args, **kwargs):
        seen.append(kwargs.get("batch_id"))
        return types.SimpleNamespace(id="r", status="processing")

    async def fake_gen_title(*args, **kwargs):
        return "Generated title"

    async def fake_get_owned_or_shared(self, ctx, action_graph_id):
        return _graph_doc()

    async def fake_create_batch(**kwargs):
        return types.SimpleNamespace(id="batch-1")

    monkeypatch.setattr(
        uag_mod.ActionGraph, "run_in_background", staticmethod(fake_run_in_background)
    )
    monkeypatch.setattr(uag_mod.LlmFunc, "gen_title", staticmethod(fake_gen_title))
    # The batch document is written before the runs; stub that write.
    monkeypatch.setattr(uag_mod.BatchService, "create_batch_for_run", fake_create_batch)
    # the inner hop re-fetches the graph; stub it rather than hit the DB
    monkeypatch.setattr(
        uag_mod.UserActionGraphs, "get_owned_or_shared", fake_get_owned_or_shared
    )

    user_graphs = uag_mod.UserActionGraphs.__new__(uag_mod.UserActionGraphs)
    user_graphs.space_member = None
    user_graphs.db = None
    user_graphs.user = types.SimpleNamespace(id="u1", email="u1@example.com")
    user_graphs.user_id = "u1"

    await user_graphs.batch_run_in_background(
        ctx=types.SimpleNamespace(user_id="u1"),
        app_state=types.SimpleNamespace(),
        user_actions=types.SimpleNamespace(db=None, user=types.SimpleNamespace(id="u1")),
        user_action_graph_result=types.SimpleNamespace(),
        action_graph_id="graph-1",
        input=DataBlock(urls=["http://a.com", "http://b.com", "http://c.com"]),
        batch_size=1,
        action_graph_doc=_graph_doc(),
    )

    assert len(seen) == 3, "expected one run per URL at batch_size=1"
    assert len(set(seen)) == 1, f"batches must share one id, got {set(seen)}"
    assert seen[0] is not None


@pytest.mark.asyncio
async def test_graph_init_puts_batch_id_on_the_create(monkeypatch):
    from src.autobots.action_graph.action_graph_result import (
        user_action_graph_result as uagr_mod,
    )

    captured = {}

    async def fake_create(self, ctx, action_graph_result):
        captured["create"] = action_graph_result
        return types.SimpleNamespace(id="graph-result-1")

    monkeypatch.setattr(uagr_mod.UserActionGraphResult, "create_action_graph_result", fake_create)

    service = uagr_mod.UserActionGraphResult.__new__(uagr_mod.UserActionGraphResult)
    service.space_member = None
    service.user_id = "u1"

    await service.init(
        app_state=types.SimpleNamespace(),
        action_graph_result_id=None,
        action_graph_doc=_graph_doc(),
        action_graph_input_dict={"text": "hi"},
        title_override="1/3 Generated title",
        db=None,
        user=types.SimpleNamespace(id="u1"),
        ctx=types.SimpleNamespace(user_id="u1"),
        batch_id="b-graph",
    )

    assert captured["create"].batch_id == "b-graph"
    assert captured["create"].name == "1/3 Generated title"


@pytest.mark.asyncio
async def test_graph_init_without_batch_id_leaves_it_none(monkeypatch):
    from src.autobots.action_graph.action_graph_result import (
        user_action_graph_result as uagr_mod,
    )

    captured = {}

    async def fake_create(self, ctx, action_graph_result):
        captured["create"] = action_graph_result
        return types.SimpleNamespace(id="graph-result-1")

    monkeypatch.setattr(uagr_mod.UserActionGraphResult, "create_action_graph_result", fake_create)

    service = uagr_mod.UserActionGraphResult.__new__(uagr_mod.UserActionGraphResult)
    service.space_member = None
    service.user_id = "u1"

    await service.init(
        app_state=types.SimpleNamespace(),
        action_graph_result_id=None,
        action_graph_doc=_graph_doc(),
        action_graph_input_dict={"text": "hi"},
        title_override="A normal run",
        db=None,
        user=types.SimpleNamespace(id="u1"),
        ctx=types.SimpleNamespace(user_id="u1"),
    )

    assert captured["create"].batch_id is None


@pytest.mark.asyncio
async def test_team_batch_run_forwards_one_shared_batch_id(monkeypatch):
    from src.autobots.action_team.action_team import user_action_team as uat_mod

    seen: list[str | None] = []

    async def fake_run_in_background(*args, **kwargs):
        seen.append(kwargs.get("batch_id"))
        return types.SimpleNamespace(id="r", status="processing")

    async def fake_create_batch(**kwargs):
        return types.SimpleNamespace(id="batch-1")

    monkeypatch.setattr(
        uat_mod.ActionTeam, "run_in_background", staticmethod(fake_run_in_background)
    )
    monkeypatch.setattr(uat_mod.BatchService, "create_batch_for_run", fake_create_batch)

    user_teams = uat_mod.UserActionTeams.__new__(uat_mod.UserActionTeams)
    user_teams.space_member = None
    user_teams.db = None
    user_teams.user = types.SimpleNamespace(id="u1")

    await user_teams.batch_run_in_background(
        ctx=types.SimpleNamespace(user_id="u1"),
        app_state=types.SimpleNamespace(),
        user_actions=types.SimpleNamespace(db=None, user=types.SimpleNamespace(id="u1")),
        user_action_team_result=types.SimpleNamespace(),
        action_team_id="team-1",
        input=DataBlock(urls=["http://a.com", "http://b.com"]),
        batch_size=1,
        action_team_doc=_team_doc(),
    )

    assert len(seen) == 2
    assert len(set(seen)) == 1, f"batches must share one id, got {set(seen)}"
    assert seen[0] is not None


@pytest.mark.asyncio
async def test_team_single_run_with_no_urls_still_gets_a_batch_id(monkeypatch):
    from src.autobots.action_team.action_team import user_action_team as uat_mod

    seen: list[str | None] = []

    async def fake_run_in_background(*args, **kwargs):
        seen.append(kwargs.get("batch_id"))
        return types.SimpleNamespace(id="r", status="processing")

    async def fake_create_batch(**kwargs):
        return types.SimpleNamespace(id="batch-1")

    monkeypatch.setattr(
        uat_mod.ActionTeam, "run_in_background", staticmethod(fake_run_in_background)
    )
    monkeypatch.setattr(uat_mod.BatchService, "create_batch_for_run", fake_create_batch)

    user_teams = uat_mod.UserActionTeams.__new__(uat_mod.UserActionTeams)
    user_teams.space_member = None
    user_teams.db = None
    user_teams.user = types.SimpleNamespace(id="u1")

    await user_teams.batch_run_in_background(
        ctx=types.SimpleNamespace(user_id="u1"),
        app_state=types.SimpleNamespace(),
        user_actions=types.SimpleNamespace(db=None, user=types.SimpleNamespace(id="u1")),
        user_action_team_result=types.SimpleNamespace(),
        action_team_id="team-1",
        input=DataBlock(text="no urls here"),
        action_team_doc=_team_doc(),
    )

    assert len(seen) == 1
    assert seen[0] is not None


@pytest.mark.asyncio
async def test_team_init_puts_batch_id_on_the_create(monkeypatch):
    from src.autobots.action_team.action_team_result import user_action_team_result as uatr_mod

    captured = {}

    async def fake_create(self, ctx, action_team_result):
        captured["create"] = action_team_result
        return types.SimpleNamespace(id="team-result-1", model_dump=lambda: {})

    async def fake_gen_title(*args, **kwargs):
        return "Generated title"

    monkeypatch.setattr(uatr_mod.UserActionTeamResult, "create_action_team_result", fake_create)
    monkeypatch.setattr(uatr_mod.LlmFunc, "gen_title", staticmethod(fake_gen_title))

    service = uatr_mod.UserActionTeamResult.__new__(uatr_mod.UserActionTeamResult)
    service.space_member = None
    service.user_id = "u1"

    await service.init(
        app_state=types.SimpleNamespace(),
        action_team_result_id=None,
        action_team_doc=_team_doc(),
        input_dict={"text": "hi"},
        db=None,
        user=types.SimpleNamespace(id="u1"),
        ctx=types.SimpleNamespace(user_id="u1"),
        batch_id="b-team",
    )

    assert captured["create"].batch_id == "b-team"
