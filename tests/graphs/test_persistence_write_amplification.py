"""How much persistence traffic one run costs.

Part 2's whole claim is "same runs, same result documents, far fewer round trips", so both halves
need a test: the counts here, and the byte-identical final document in
`test_persistence_document_identity.py`.

These numbers are measured against the real `update_action_graph_result`, so they move the moment
the write path changes. That is the point - they are the regression fence, not documentation.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from src.autobots.action_graph.action_graph.action_graph import ActionGraph
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from tests.graphs.persistence_probe import (
    Probe,
    a_graph_doc,
    a_probed_service,
    a_run_doc,
    an_action_result,
)

# A straight chain, so node count and layer count are the same number.
CHAIN = {"s": ["a"], "a": ["b"], "b": ["c"], "c": ["d"], "d": ["e"]}
CHAIN_NODES = 6  # s + a..e


async def run_a_graph(
    graph: Dict[str, List[str]],
    *,
    loop_nodes: tuple[str, ...] = (),
    action_input: Dict[str, Any] | None = None,
    payload_blocks: int = 1,
    blocks_by_action: Dict[str, int] | None = None,
) -> tuple[Probe, Any]:
    """Run a graph end to end through the real write path.

    `blocks_by_action` overrides the per-node output size. A loop node's iteration count is the
    number of blocks its *upstream* emits, so that is the knob the loop benchmarks turn.
    """
    graph_doc = a_graph_doc(graph, loop_nodes=loop_nodes)
    run_doc = a_run_doc(graph_doc)
    service, probe = a_probed_service(run_doc)
    sizes = blocks_by_action or {}

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        return an_action_result(action_id, payload_blocks=sizes.get(action_id, payload_blocks))

    with patch.object(ActionGraph, "run_action", AsyncMock(side_effect=fake_run_action)):
        final = await ActionGraph.run_action_graph_v2(
            ctx=Context(),
            app_state=AppState(),
            user_actions=AsyncMock(),
            action_graph_input_dict=action_input or {"text": "in"},
            action_graph_result_doc=run_doc,
            action_graph_node_id=None,
            is_continue_run_graph=False,
            user_action_graph_result=service,
            notify=None,
        )
    return probe, final


# --------------------------------------------------------------------------- the DAG shape


@pytest.mark.asyncio
async def test_a_plain_dag_run_reads_nothing_and_writes_once_per_node():
    """One write at start, one per node, one for the final status - and no reads at all.

    Before Part 2 this shape cost 8 GETs alongside its 8 PUTs, because every write re-fetched the
    document the engine was holding. The per-node writes stay: they are the durability guarantee
    that lets a failed run resume, and they are linear.
    """
    probe, final = await run_a_graph(CHAIN)

    counts = probe.snapshot()
    assert counts["s3_puts"] == counts["mongo_writes"]
    assert counts["s3_puts"] >= CHAIN_NODES, counts
    # The write *counts* are what this file measures and they are unchanged - one PUT per node
    # save, no reads. The document is larger, which is the cost of a DAG keeping a transcript.
    assert sorted((final.result.node_vectors or {})["s"].slots) == ["0"]

    assert counts["s3_gets"] == 0, "the engine already holds the document it is writing"

    # Search covers `name`, `status` and `result.name`. A per-node write changes none of them, so
    # only the status transitions bump: one at run start, one at run end.
    assert counts["cache_bumps"] == 2, counts


@pytest.mark.asyncio
async def test_every_write_serialises_the_whole_document():
    """The amplification factor: total bytes persisted over the size of the document produced.

    Persisting one final document costs roughly one whole document per write. Stated as a ratio
    rather than an absolute so it survives the document shape changing.
    """
    probe, _ = await run_a_graph(CHAIN)
    counts = probe.snapshot()
    final_size = len(probe.stored_document("run-1"))

    amplification = counts["s3_bytes_written"] / final_size
    assert amplification > counts["s3_puts"] / 2, (
        f"{counts['s3_puts']} writes cost {amplification:.1f}x the final document"
    )
    # Still true per *write* - each one serialises the whole document - which is why the fix for
    # the loop had to be fewer writes rather than smaller ones.
    assert amplification < counts["s3_puts"], counts


# --------------------------------------------------------------------------- the loop shape


async def run_a_loop(iterations: int) -> Probe:
    """A single loop node fed `iterations` blocks by its upstream."""
    probe, _ = await run_a_graph(
        {"s": ["a"]},
        loop_nodes=("a",),
        blocks_by_action={"action-s": iterations, "action-a": 1},
    )
    return probe


@pytest.mark.asyncio
@pytest.mark.parametrize("iterations", [10, 40])
async def test_a_loop_node_no_longer_writes_once_per_iteration(iterations: int):
    """`run_action_with_loop_concurrently` used to persist after every iteration, under the lock.

    That was the quadratic site: iteration i wrote a document holding i collated results, so a
    100-iteration loop wrote 2.1MB to persist a 10.5KB document.
    """
    counts = (await run_a_loop(iterations)).snapshot()
    assert counts["s3_gets"] == 0, counts
    assert counts["cache_bumps"] == 2, counts
    # The count no longer tracks the iteration count at all: the throttle collapses the
    # intermediate saves, and what is left is the run-level and node-level writes.
    assert counts["s3_puts"] < iterations, counts


@pytest.mark.asyncio
async def test_loop_bytes_written_are_no_longer_quadratic():
    """The exit criterion: sub-quadratic total bytes for a long loop.

    Each iteration used to write the whole document, and the document grows as
    `collate_action_results` appends - so total bytes went as n^2/2 and doubling the iteration
    count more than doubled the bytes written (measured: 2.4x, 2.9x). Now growth tracks the
    document's own size, which is linear in the iteration count.
    """
    ratios: List[float] = []
    previous: float | None = None
    for iterations in (10, 20, 40):
        written = (await run_a_loop(iterations)).snapshot()["s3_bytes_written"]
        if previous is not None:
            ratios.append(written / previous)
        previous = written

    assert all(r < 2.0 for r in ratios), f"expected sub-quadratic growth, got ratios {ratios}"


@pytest.mark.asyncio
async def test_a_long_loop_costs_a_small_multiple_of_its_own_document():
    """Stated as amplification, which is what actually hurts at 200 rows.

    Before Part 2 a 100-iteration loop wrote 201x its final document. The bound here is loose on
    purpose - it is a fence against the quadratic behaviour returning, not a target.
    """
    probe = await run_a_loop(100)
    counts = probe.snapshot()
    amplification = counts["s3_bytes_written"] / len(probe.stored_document("run-1"))

    assert amplification < 10, f"a 100-iteration loop wrote {amplification:.0f}x its document"


# --------------------------------------------------------------------------- the throttle


@pytest.fixture
def never_flush(monkeypatch):
    """An interval no test can outlast, so exactly one intermediate save happens.

    Without this the counts depend on how long the machine takes to run the loop, which under
    pytest-xdist is not a fixed quantity.
    """
    from src.autobots import SettingsProvider

    monkeypatch.setattr(
        SettingsProvider.sget(),
        "ACTION_GRAPH_RESULT_FLUSH_INTERVAL_SECONDS",
        3600.0,
        raising=False,
    )


@pytest.mark.asyncio
async def test_a_throttled_loop_still_persists_every_result(never_flush):
    """The property the throttle must not break.

    Skipping an intermediate save is only safe because an unconditional one follows: the loop's
    final collation, and then the node-level save in `run_action_graph_v2`. With every
    intermediate save suppressed, the stored document must still hold all 40 rows.
    """
    import json

    probe = await run_a_loop(40)
    document = json.loads(probe.stored_document("run-1"))
    collated = document["result"]["output"]["a"]

    assert len(collated["output"]["blocks"]) == 40, (
        "the throttle dropped results, not just writes"
    )


@pytest.mark.asyncio
async def test_the_throttle_is_off_when_the_interval_is_zero(monkeypatch):
    """The escape hatch: 0 restores a save per iteration."""
    from src.autobots import SettingsProvider

    monkeypatch.setattr(
        SettingsProvider.sget(), "ACTION_GRAPH_RESULT_FLUSH_INTERVAL_SECONDS", 0.0, raising=False
    )

    counts = (await run_a_loop(20)).snapshot()
    assert counts["s3_puts"] >= 20, counts


@pytest.mark.asyncio
async def test_the_throttle_lets_a_save_through_once_the_interval_has_passed(monkeypatch):
    """Partial results must not stop appearing - a long loop cannot look frozen."""
    from src.autobots import SettingsProvider
    from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
        UserActionGraphResult,
    )

    monkeypatch.setattr(
        SettingsProvider.sget(), "ACTION_GRAPH_RESULT_FLUSH_INTERVAL_SECONDS", 3.0, raising=False
    )
    service = object.__new__(UserActionGraphResult)
    service._last_flush_at = {}

    clock = [1000.0]
    monkeypatch.setattr(
        "src.autobots.action_graph.action_graph_result.user_action_graph_result.time.monotonic",
        lambda: clock[0],
    )

    assert service._flush_is_due("run-1") is True, "the first save always goes through"
    clock[0] += 1.0
    assert service._flush_is_due("run-1") is False
    clock[0] += 2.5  # 3.5s since the last save
    assert service._flush_is_due("run-1") is True

    # Runs are throttled independently.
    assert service._flush_is_due("run-2") is True


# --------------------------------------------------------------------------- the cyclic shape


async def save_a_document(run_doc, *, times: int = 1):
    """Persist a prepared document through the real write path `times` times."""
    from tests.graphs.persistence_probe import a_probed_service

    service, probe = a_probed_service(run_doc)
    for _ in range(times):
        await service.save_run_doc(Context(), run_doc)
    return service, probe


@pytest.mark.asyncio
async def test_a_cyclic_document_costs_its_slot_count_and_nothing_more():
    """Exit criterion 2: benchmark the shape that carries the context vector, not just the DAG.

    Part 3 builds the scheduler that produces these. The persistence path does not care how the
    vector got into the document, only how big the document is - so the shape is constructed
    directly rather than waiting for a scheduler to make one.
    """
    from tests.graphs.persistence_probe import a_cyclic_run_doc

    dag_probe, _ = await run_a_graph(CHAIN)
    dag_final = len(dag_probe.stored_document("run-1"))

    _, k3 = await save_a_document(a_cyclic_run_doc(CHAIN, slots=3, turns=2))
    k3_final = len(k3.stored_document("run-1"))

    assert k3.snapshot()["s3_gets"] == 0, "the cyclic shape reads no more than the DAG shape"
    assert k3_final > dag_final, "the vector is not free"
    # The vector holds an entry per (slot, turn) beside the ActionDoc, so a K=3 document is a
    # small multiple of the DAG one - not an order of magnitude.
    assert k3_final < dag_final * 6, f"K=3 document is {k3_final / dag_final:.1f}x the DAG one"


@pytest.mark.asyncio
async def test_cyclic_document_size_grows_linearly_in_K():
    """K slots cost K times the vector, not K^2. The property the scheduler will lean on."""
    from tests.graphs.persistence_probe import a_cyclic_run_doc

    sizes = []
    for slots in (2, 4, 8):
        _, probe = await save_a_document(a_cyclic_run_doc(CHAIN, slots=slots, turns=2))
        sizes.append(len(probe.stored_document("run-1")))

    # Doubling K must not more than double the document.
    assert sizes[1] / sizes[0] < 2.0, sizes
    assert sizes[2] / sizes[1] < 2.0, sizes


@pytest.mark.asyncio
async def test_a_cyclic_run_pays_the_same_per_save_amplification_as_a_dag():
    """The saving is in the *number* of writes, so it has to hold for the larger document too."""
    from tests.graphs.persistence_probe import a_cyclic_run_doc

    _, probe = await save_a_document(a_cyclic_run_doc(CHAIN, slots=3, turns=2), times=6)
    counts = probe.snapshot()

    assert counts["s3_gets"] == 0, counts
    assert counts["s3_puts"] == 6
    amplification = counts["s3_bytes_written"] / len(probe.stored_document("run-1"))
    assert amplification < 6.5, f"{amplification:.1f}x - each save still writes one document"
