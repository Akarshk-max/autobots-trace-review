from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from src.autobots.run_metrics.run_metrics_models import ModelTotals
from src.autobots.run_metrics.run_metrics_scope import RunMetricsScope, ScopeKind
from src.autobots.run_metrics.run_metrics_service import RunMetricsService
from src.autobots.run_metrics.run_metrics_writer import RunMetricsWriter

USER = "per-model-user"
AT = datetime.now() - timedelta(days=1)
BIG, SMALL = "gpt-5.2", "claude-opus-5"


def _model_share(model, tokens, cost):
    return {
        "model": model, "tokens_input": tokens // 2, "tokens_output": tokens // 2,
        "tokens_cached": 0, "tokens_cache_write": 0, "tokens_reasoning": 0,
        "tokens_total": tokens, "llm_cost_usd": cost, "llm_cost_complete": True,
    }


def _row(run_id, shares):
    return {
        "_id": run_id, "user_id": USER, "space_id": None, "type": "action_graph_run",
        "status": "success", "created_at": AT, "finished_at": AT, "subject_id": "wf-1",
        "units_total": 10, "units_succeeded": 10, "hours_saved": 5.0, "cost_per_hour": 100.0,
        "files_in": 3, "files_out": 3, "duration_seconds": 30.0, "is_root_run": True,
        "tokens_total": sum(s["tokens_total"] for s in shares),
        "llm_cost_usd": sum(s["llm_cost_usd"] for s in shares),
        "llm_cost_complete": True, "trigger": "api", "error_class": None,
        "totals_by_model": shares,
    }


@pytest_asyncio.fixture
async def seeded(db):
    docs = [
        _row("rm-pm-split", [_model_share(BIG, 1000, 0.50), _model_share(SMALL, 900, 0.40)]),
        _row("rm-pm-single", [_model_share(BIG, 200, 0.10)]),
    ]
    await db["RunMetrics"].insert_many(docs)
    yield db
    await db["RunMetrics"].delete_many({"_id": {"$in": [d["_id"] for d in docs]}})


async def _by_model(database):
    rows = await RunMetricsService(database).by_model(
        RunMetricsScope(ScopeKind.personal, USER), AT - timedelta(days=1), AT + timedelta(days=1)
    )
    return {row["model"]: row for row in rows}


@pytest.mark.asyncio
async def test_each_model_is_charged_only_its_own_spend(seeded):
    rows = await _by_model(seeded)
    assert rows[BIG]["llm_cost_usd"] == pytest.approx(0.60), "0.50 from the split run + 0.10"
    assert rows[SMALL]["llm_cost_usd"] == pytest.approx(0.40)


@pytest.mark.asyncio
async def test_each_model_carries_only_its_own_tokens(seeded):
    rows = await _by_model(seeded)
    assert rows[BIG]["tokens_total"] == 1200
    assert rows[SMALL]["tokens_total"] == 900


@pytest.mark.asyncio
async def test_a_model_counts_the_runs_it_took_part_in(seeded):
    rows = await _by_model(seeded)
    assert rows[BIG]["runs"] == 2, "it was used by both runs"
    assert rows[SMALL]["runs"] == 1, "it was used by the split run only"


@pytest.mark.asyncio
async def test_the_models_add_up_to_the_run_totals(seeded):
    """The breakdown and the headline come from the same rows, so they must reconcile."""
    rows = await _by_model(seeded)
    totals = await RunMetricsService(seeded).totals(
        RunMetricsScope(ScopeKind.personal, USER), AT - timedelta(days=1), AT + timedelta(days=1)
    )
    assert sum(r["tokens_total"] for r in rows.values()) == totals["tokens_total"]
    assert sum(r["llm_cost_usd"] for r in rows.values()) == pytest.approx(totals["llm_cost_usd"])


def test_a_model_is_not_credited_with_the_runs_value():
    """Hours saved, money saved, units and files belong to the run, not to a model."""
    owned = set(ModelTotals.model_fields)
    assert not owned & {
        "hours_saved", "money_saved", "units_total", "units_succeeded",
        "files_in", "files_out", "duration_p50", "duration_p95", "succeeded",
    }


@pytest.mark.asyncio
async def test_the_writer_splits_one_runs_rows_across_its_models():
    """The run total and the per-model shares come from one pass, so they cannot disagree."""
    groups = [
        {"_id": BIG, "rows": 2, "priced_rows": 2, "cost": 0.50, "tokens_input": 300,
         "tokens_output": 200, "tokens_cached": 0, "tokens_cache_write": 0,
         "tokens_reasoning": 0, "tokens_total": 500},
        {"_id": SMALL, "rows": 1, "priced_rows": 1, "cost": 0.25, "tokens_input": 100,
         "tokens_output": 100, "tokens_cached": 0, "tokens_cache_write": 0,
         "tokens_reasoning": 0, "tokens_total": 200},
    ]

    class _Cursor:
        def __aiter__(self):
            async def gen():
                for g in groups:
                    yield g
            return gen()

    writer = RunMetricsWriter(None)
    writer.token_usage_crud = SimpleNamespace(
        document=SimpleNamespace(aggregate=lambda pipeline: _Cursor())
    )

    run_totals, by_model = await writer._token_totals({"action_graph_result_id": "r-1"})

    assert run_totals.tokens_total == 700
    assert run_totals.llm_cost_usd == pytest.approx(0.75)
    assert run_totals.llm_cost_complete is True
    assert {m.model: m.tokens_total for m in by_model} == {BIG: 500, SMALL: 200}
    assert sum(m.tokens_total for m in by_model) == run_totals.tokens_total
