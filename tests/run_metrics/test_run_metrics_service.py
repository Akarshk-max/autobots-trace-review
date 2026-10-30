from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from src.autobots.run_metrics.run_metrics_scope import RunMetricsScope, ScopeKind
from src.autobots.run_metrics.run_metrics_service import RunMetricsService

USER = "run-metrics-test-user"
SPACE = "run-metrics-test-space"
AT = datetime.now() - timedelta(days=1)


def _run(run_id, *, user=USER, space=None, status="success", hours=1.0, rate=50.0, cost=0.10):
    return {
        "_id": run_id, "user_id": user, "space_id": space, "type": "action_graph_run",
        "status": status, "created_at": AT, "finished_at": AT, "subject_id": "wf-1",
        "units_total": 10, "units_succeeded": 8, "hours_saved": hours, "cost_per_hour": rate,
        "files_in": 2, "files_out": 1, "duration_seconds": 30.0,
        "tokens_total": 100, "llm_cost_usd": cost, "trigger": "api", "error_class": None,
        "llm_cost_complete": cost is not None,
    }


@pytest_asyncio.fixture
async def seeded(db):
    docs = [
        _run("rm-t-ok"),
        _run("rm-t-failed", status="error", hours=0.0),
        _run("rm-t-unpriced", cost=None),
        _run("rm-t-other-user", user="run-metrics-other-user", space=SPACE),
        _run("rm-t-mine-in-space", space=SPACE),
    ]
    await db["RunMetrics"].insert_many(docs)
    yield db
    await db["RunMetrics"].delete_many({"_id": {"$in": [d["_id"] for d in docs]}})


def _window():
    return AT - timedelta(days=1), AT + timedelta(days=1)


class TestTotals:
    @pytest.mark.asyncio
    async def test_a_failed_run_counts_as_a_run_but_saves_nothing(self, seeded):
        start, end = _window()
        totals = await RunMetricsService(seeded).totals(
            RunMetricsScope(ScopeKind.personal, USER), start, end
        )
        assert totals["runs"] == 3
        assert totals["succeeded"] == 2
        assert totals["hours_saved"] == 2.0

    @pytest.mark.asyncio
    async def test_money_uses_each_run_own_rate(self, seeded):
        start, end = _window()
        totals = await RunMetricsService(seeded).totals(
            RunMetricsScope(ScopeKind.personal, USER), start, end
        )
        assert totals["money_saved"] == 100.0

    @pytest.mark.asyncio
    async def test_partial_cost_coverage_is_reported_not_hidden(self, seeded):
        start, end = _window()
        totals = await RunMetricsService(seeded).totals(
            RunMetricsScope(ScopeKind.personal, USER), start, end
        )
        assert totals["llm_cost_runs"] == 2
        assert totals["llm_cost_complete"] is False

    @pytest.mark.asyncio
    async def test_an_empty_window_returns_zeroes_rather_than_failing(self, db):
        far_past = datetime(2000, 1, 1)
        totals = await RunMetricsService(db).totals(
            RunMetricsScope(ScopeKind.personal, "nobody-at-all"), far_past, far_past
        )
        assert totals["runs"] == 0
        assert totals["llm_cost_usd"] is None
        assert totals["duration_p50"] is None


class TestScopeIsolation:
    @pytest.mark.asyncio
    async def test_a_member_never_sees_another_members_runs(self, seeded):
        start, end = _window()
        totals = await RunMetricsService(seeded).totals(
            RunMetricsScope(ScopeKind.own_in_space, USER, SPACE), start, end
        )
        assert totals["runs"] == 1

    @pytest.mark.asyncio
    async def test_an_admin_sees_every_members_runs(self, seeded):
        start, end = _window()
        totals = await RunMetricsService(seeded).totals(
            RunMetricsScope(ScopeKind.whole_space, USER, SPACE), start, end
        )
        assert totals["runs"] == 2

    @pytest.mark.asyncio
    async def test_personal_excludes_work_done_inside_a_space(self, seeded):
        start, end = _window()
        totals = await RunMetricsService(seeded).totals(
            RunMetricsScope(ScopeKind.personal, USER), start, end
        )
        assert totals["runs"] == 3
