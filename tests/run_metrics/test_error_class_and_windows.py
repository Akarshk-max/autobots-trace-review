from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from src.autobots.action.action.common_action_models import TextObj
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.run_metrics.run_metrics_scope import RunMetricsScope, ScopeKind
from src.autobots.run_metrics.run_metrics_service import RunMetricsService
from src.autobots.run_metrics.run_metrics_writer import RunMetricsWriter

USER = "err-window-user"
AT = datetime.now() - timedelta(days=1)


def _result(status, text):
    return SimpleNamespace(status=status, error_message=TextObj(text=text))


class TestErrorClass:
    def test_a_cancelled_run_gets_no_error_class(self):
        """The message is literally "Cancelled", which `.isidentifier()` accepts."""
        run = _result(EventResultStatus.cancelled, "Cancelled")
        assert RunMetricsWriter._error_class(run) is None

    def test_a_real_failure_still_gets_its_exception_type(self):
        run = _result(EventResultStatus.error, "TimeoutError: upstream went away")
        assert RunMetricsWriter._error_class(run) == "TimeoutError"

    def test_a_success_gets_no_error_class(self):
        assert RunMetricsWriter._error_class(_result(EventResultStatus.success, "")) is None


def _row(run_id, status, error_class):
    return {
        "_id": run_id, "user_id": USER, "space_id": None, "type": "action_run",
        "status": status, "created_at": AT, "finished_at": AT, "subject_id": "s-1",
        "units_total": 1, "units_succeeded": 0, "hours_saved": 0.0, "cost_per_hour": 50.0,
        "files_in": 0, "files_out": 0, "duration_seconds": 1.0, "tokens_total": 0,
        "llm_cost_usd": 0.0, "llm_cost_complete": True, "trigger": "api",
        "error_class": error_class, "is_root_run": True, "totals_by_model": [],
    }


@pytest_asyncio.fixture
async def seeded(db):
    docs = [
        _row("rm-ec-cancelled", "cancelled", "Cancelled"),  # as older code would have written it
        _row("rm-ec-failed", "error", "TimeoutError"),
    ]
    await db["RunMetrics"].insert_many(docs)
    yield db
    await db["RunMetrics"].delete_many({"_id": {"$in": [d["_id"] for d in docs]}})


@pytest.mark.asyncio
async def test_a_cancellation_never_reaches_the_top_errors(seeded):
    """The read side also excludes it, so rows already written this way stay out."""
    errors = await RunMetricsService(seeded).by_error_class(
        RunMetricsScope(ScopeKind.personal, USER), AT - timedelta(days=1), AT + timedelta(days=1)
    )
    classes = {row["error_class"] for row in errors}
    assert "Cancelled" not in classes
    assert classes == {"TimeoutError"}


@pytest.mark.asyncio
async def test_the_cancellation_is_still_counted_as_cancelled(seeded):
    totals = await RunMetricsService(seeded).totals(
        RunMetricsScope(ScopeKind.personal, USER), AT - timedelta(days=1), AT + timedelta(days=1)
    )
    assert totals["cancelled"] == 1
    assert totals["runs"] == 2


class TestWindows:
    """Half-open bounds, so the comparison arrows compare disjoint periods."""

    def _windows(self):
        end = datetime(2026, 9, 8, 12, 0, 0)
        start = end - timedelta(days=7)
        scope = RunMetricsScope(ScopeKind.personal, USER)
        return start, end, scope.match(start - (end - start), start), scope.match(start, end)

    @staticmethod
    def _contains(match, moment):
        bounds = match["created_at"]
        return bounds["$gte"] <= moment < bounds["$lt"]

    @pytest.mark.parametrize("offset_seconds", [-604800, -1, 0, 604799])
    def test_every_run_belongs_to_exactly_one_window(self, offset_seconds):
        start, _, previous, current = self._windows()
        moment = start + timedelta(seconds=offset_seconds)
        assert self._contains(previous, moment) != self._contains(current, moment)

    def test_a_run_exactly_on_the_boundary_is_not_counted_twice(self):
        start, _, previous, current = self._windows()
        assert not self._contains(previous, start)
        assert self._contains(current, start)
