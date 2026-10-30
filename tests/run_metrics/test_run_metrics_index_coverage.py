from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from src.autobots.run_metrics.run_metrics_crud import RunMetricsCRUD
from src.autobots.run_metrics.run_metrics_scope import RunMetricsScope, ScopeKind

USER = "coverage-user"
SPACE = "coverage-space"
NOW = datetime.now()
IN_WINDOW = 20
OUT_OF_WINDOW = 400
TYPES = ("action_run", "action_graph_run", "action_team_run")

SCOPES = [
    RunMetricsScope(ScopeKind.personal, USER),
    RunMetricsScope(ScopeKind.own_in_space, USER, SPACE),
    RunMetricsScope(ScopeKind.whole_space, USER, SPACE),
]


def _row(index, days_ago, space):
    return {
        "user_id": USER, "space_id": space, "type": TYPES[index % len(TYPES)],
        "status": "success", "is_root_run": True,
        "created_at": NOW - timedelta(days=days_ago),
        "finished_at": NOW - timedelta(days=days_ago), "subject_id": f"subj-{index % 7}",
        "units_total": 1, "units_succeeded": 1, "hours_saved": 1.0, "cost_per_hour": 50.0,
        "files_in": 0, "files_out": 0, "duration_seconds": 5.0, "tokens_total": 10,
        "llm_cost_usd": 0.01, "llm_cost_complete": True, "trigger": "api", "error_class": None,
    }


COLLECTION = "RunMetricsIndexCoverageTest"


@pytest_asyncio.fixture
async def seeded(db):
    collection = db[COLLECTION]
    await collection.drop()
    docs = []
    for space in (None, SPACE):
        docs += [_row(i, 1 + i % 5, space) for i in range(IN_WINDOW)]
        docs += [_row(i, 120 + i % 300, space) for i in range(OUT_OF_WINDOW)]
    for position, doc in enumerate(docs):
        doc["_id"] = f"rm-cov-{position}"
    await collection.insert_many(docs)
    await collection.create_indexes(RunMetricsCRUD.__indexes__)
    yield db
    await collection.drop()


def _execution_stats(explain: dict) -> dict:
    stages = explain.get("stages")
    if stages:
        return stages[0]["$cursor"]["executionStats"]
    return explain["executionStats"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", SCOPES, ids=[s.kind.value for s in SCOPES])
async def test_a_window_query_does_not_walk_the_whole_history(seeded, scope):
    match = scope.match(NOW - timedelta(days=7), NOW)
    explain = await seeded.command(
        "explain",
        {"aggregate": COLLECTION, "pipeline": [{"$match": match}], "cursor": {}},
        verbosity="executionStats",
    )
    stats = _execution_stats(explain)
    returned = stats["nReturned"]

    assert returned > 0, "the window should match the seeded recent rows"
    slack = returned + 3 * len(TYPES)  # one index sub-scan per run type
    # Both are needed: a badly shaped index shows up in the keys examined, while no usable index
    # at all falls back to a collection scan, where no keys are examined but every document is.
    assert stats["totalKeysExamined"] <= slack, (
        f"{scope.kind.value}: examined {stats['totalKeysExamined']} keys for {returned} rows"
    )
    assert stats["totalDocsExamined"] <= slack, (
        f"{scope.kind.value}: read {stats['totalDocsExamined']} documents for {returned} rows"
    )


@pytest.mark.asyncio
async def test_the_declared_indexes_carry_no_unqueried_payload(db):
    """They once trailed eight metric fields chasing a covered query that never happened; the
    fields `_totals()` reads were never all present, so the payload only slowed writes."""
    keys = {name for index in RunMetricsCRUD.__indexes__ for name, _ in index.document["key"].items()}
    assert keys <= {"user_id", "space_id", "is_root_run", "status", "created_at"}


@pytest.mark.asyncio
async def test_every_declared_index_is_creatable(db):
    """A malformed declaration would otherwise only surface at startup, where it is swallowed."""
    assert await RunMetricsCRUD(db).document.create_indexes(RunMetricsCRUD.__indexes__)
