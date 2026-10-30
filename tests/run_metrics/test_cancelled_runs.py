from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from src.autobots.run_metrics.run_metrics_models import Totals
from src.autobots.run_metrics.run_metrics_scope import RunMetricsScope, ScopeKind
from src.autobots.run_metrics.run_metrics_service import RunMetricsService

USER = "run-metrics-cancel-user"
AT = datetime.now() - timedelta(days=1)


def _row(run_id, status, cost, tokens):
    return {
        "_id": run_id, "user_id": USER, "space_id": None, "type": "action_run",
        "status": status, "created_at": AT, "finished_at": AT, "subject_id": "subj-1",
        "units_total": 1, "units_succeeded": 1 if status == "success" else 0,
        "hours_saved": 1.0 if status == "success" else 0.0, "cost_per_hour": 50.0,
        "files_in": 0, "files_out": 0, "duration_seconds": 5.0,
        "tokens_total": tokens, "llm_cost_usd": cost, "llm_cost_complete": True,
        "trigger": "api", "error_class": None, "is_root_run": True,
    }


@pytest_asyncio.fixture
async def seeded(db):
    docs = [
        _row("rm-cancel-ok", "success", 0.10, 100),
        _row("rm-cancel-err", "error", 0.05, 50),
        _row("rm-cancel-stopped", "cancelled", 0.02, 20),
    ]
    await db["RunMetrics"].insert_many(docs)
    yield db
    await db["RunMetrics"].delete_many({"_id": {"$in": [d["_id"] for d in docs]}})


async def _totals(database):
    return await RunMetricsService(database).totals(
        RunMetricsScope(ScopeKind.personal, USER), AT - timedelta(days=1), AT + timedelta(days=1)
    )


@pytest.mark.asyncio
async def test_a_cancelled_run_is_visible_at_all(seeded):
    totals = await _totals(seeded)
    assert totals["runs"] == 3
    assert totals["cancelled"] == 1


@pytest.mark.asyncio
async def test_the_money_a_cancelled_run_spent_is_still_counted(seeded):
    """It was billed. Hiding it understates spend."""
    totals = await _totals(seeded)
    assert totals["llm_cost_usd"] == pytest.approx(0.17)
    assert totals["tokens_total"] == 170


@pytest.mark.asyncio
async def test_a_cancellation_is_not_reported_as_a_failure(seeded):
    shaped = Totals(**await _totals(seeded))
    assert shaped.failed == 1, "only the errored run failed"
    assert shaped.cancelled == 1


@pytest.mark.asyncio
async def test_cancelling_a_run_does_not_depress_the_success_rate(seeded):
    """One success and one error finished; the cancelled run never got its chance."""
    shaped = Totals(**await _totals(seeded))
    assert shaped.success_rate == pytest.approx(0.5)


def test_a_window_of_only_cancellations_has_no_success_rate():
    shaped = Totals(runs=2, succeeded=0, cancelled=2)
    assert shaped.success_rate is None
    assert shaped.failed == 0
