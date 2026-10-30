from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from src.autobots.run_metrics.run_metrics_scope import RunMetricsScope, ScopeKind
from src.autobots.run_metrics.run_metrics_service import RunMetricsService

USER = "run-metrics-nesting-user"
AT = datetime.now() - timedelta(days=1)
MEMBER_COST = 0.25
LEADER_COST = 0.10
TEAM_COST = MEMBER_COST + LEADER_COST

def _row(run_id, run_type, cost, tokens, *, is_root_run):
    return {
        "_id": run_id, "user_id": USER, "space_id": None, "type": run_type,
        "status": "success", "created_at": AT, "finished_at": AT, "subject_id": "subj-1",
        "units_total": 1, "units_succeeded": 1, "hours_saved": 1.0, "cost_per_hour": 50.0,
        "files_in": 0, "files_out": 0, "duration_seconds": 10.0,
        "tokens_total": tokens, "llm_cost_usd": cost, "llm_cost_complete": True,
        "trigger": "api", "error_class": None, "is_root_run": is_root_run,
    }


@pytest_asyncio.fixture
async def team_with_a_member(db):
    docs = [
        _row("rm-nest-team", "action_team_run", TEAM_COST, 300, is_root_run=True),
        _row("rm-nest-member", "action_run", MEMBER_COST, 200, is_root_run=False),
    ]
    await db["RunMetrics"].insert_many(docs)
    yield db
    await db["RunMetrics"].delete_many({"_id": {"$in": [d["_id"] for d in docs]}})


async def _totals(database):
    return await RunMetricsService(database).totals(
        RunMetricsScope(ScopeKind.personal, USER), AT - timedelta(days=1), AT + timedelta(days=1)
    )


@pytest.mark.asyncio
async def test_the_members_cost_is_counted_once(team_with_a_member):
    totals = await _totals(team_with_a_member)
    assert totals["llm_cost_usd"] == pytest.approx(TEAM_COST), (
        "the team already includes its member's spend; adding the child row double-counts it"
    )


@pytest.mark.asyncio
async def test_the_member_is_not_counted_as_a_separate_run(team_with_a_member):
    totals = await _totals(team_with_a_member)
    assert totals["runs"] == 1
    assert totals["tokens_total"] == 300


@pytest.mark.asyncio
async def test_rows_written_before_the_field_existed_are_still_counted(db):
    """`$ne: False` rather than `True`, so a row with no `is_root_run` key does not vanish."""
    legacy = _row("rm-nest-legacy", "action_run", 0.05, 50, is_root_run=True)
    legacy.pop("is_root_run")
    await db["RunMetrics"].insert_one(legacy)
    try:
        totals = await _totals(db)
        assert totals["runs"] == 1, "a pre-existing row must not be filtered out"
    finally:
        await db["RunMetrics"].delete_one({"_id": "rm-nest-legacy"})
