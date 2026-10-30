"""Reconstructing the vectors a run would have had, for a run written before they existed.

`is_node_done` answers "in `output`, but no vector" with **not done** - at K > 1 that combination
is drift, a write that used `for_dag` where it should have carried slots. On a document written
before vectors existed it means the opposite: the node ran, before the deploy. Left alone, the
engine reads every completed node as never having run and re-executes it, billed, on the first
resume after the change.

The seed is lazy rather than a migration: it fires when a run is next touched, so historical
documents nobody reopens cost nothing and there is no S3 sweep to race with a run in flight.

Same shape Part 3.5 chose for `_turns_recorded` - the transcript is the only record of what a run
did, so it *seeds* the machine rather than the machine concluding the run did nothing.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from src.autobots.action.action.action_doc_model import ActionDoc, ActionResult
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action_graph.action_graph.action_graph import ActionGraph
from src.autobots.action_graph.action_graph.action_graph_doc_model import EdgeSpec
from src.autobots.action_graph.action_graph.context_vector import (
    BASELINE_SLOT,
    baseline_matches_action_doc,
)
from src.autobots.action_graph.action_graph.node_result_writer import (
    NodeRunContext,
    is_node_done,
    record_node_result,
    seed_baseline_from_output,
)
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from tests.graphs.persistence_probe import a_graph_doc, a_probed_service, a_run_doc

START = "s"
CHAIN: Dict[str, List[str]] = {"s": ["a"], "a": ["b"], "b": ["z"], "z": []}
REDIRECT = [EdgeSpec(source="z", target="a")]
SLOTS = (BASELINE_SLOT, "1")


def a_doc(node: str, *, runs: int = 1, latest: str | None = None) -> ActionDoc:
    """A node's stored ActionDoc with `runs` recorded executions.

    `latest` overrides the document's own output so it can be made to differ from `results[-1]` -
    which is how the last-entry rule gets tested.
    """
    results = [
        ActionResult(
            input={"text": f"in {node} {i}"}, output={"blocks": [{"text": f"{node} run {i}"}]}
        )
        for i in range(runs)
    ]
    output: Dict[str, Any] = {"blocks": [{"text": latest or f"{node} run {runs - 1}"}]}
    return ActionDoc(
        _id=f"action-{node}",
        user_id="user-1",
        name=f"action {node}",
        type=ActionType.mock_action,
        input={"text": f"in {node} {runs - 1}"},
        output=output,
        results=results,
    )


def a_pre_vector_run(**docs: ActionDoc):
    """A run document as it looked before vectors: `output` populated, `node_vectors` absent."""
    graph_doc = a_graph_doc(CHAIN)
    graph_doc.redirect_edges = REDIRECT
    run_doc = a_run_doc(graph_doc)
    for node, doc in docs.items():
        # `for_dag` writes `output[node]` and no vector - exactly the old shape.
        record_node_result(run_doc, doc, NodeRunContext.for_dag(node))
    assert run_doc.result.node_vectors is None, "the fixture is not a pre-vector document"
    return run_doc


def entries(run_doc, node: str, slot: str = BASELINE_SLOT):
    vector = (run_doc.result.node_vectors or {})[node]
    return [(e.node_id, e.turn_no, (e.output or {}).get("blocks", [{}])[0].get("text"))
            for e in vector.slot(slot).sorted_entries(START)]


# --------------------------------------------------------------------------- when it does nothing


def test_a_document_that_already_has_vectors_is_left_alone():
    """`node_vectors is not None` is the "already done" signal, which is what makes it idempotent."""
    run_doc = a_pre_vector_run(a=a_doc("a"))
    seed_baseline_from_output(run_doc, start_node_id=START, all_slots=SLOTS)
    first = entries(run_doc, "a")

    again = seed_baseline_from_output(run_doc, start_node_id=START, all_slots=SLOTS)

    assert again == [], "a second pass claimed to seed something"
    assert entries(run_doc, "a") == first, "a second pass changed the transcript"


def test_a_run_that_has_executed_nothing_is_left_alone():
    run_doc = a_pre_vector_run()

    assert seed_baseline_from_output(run_doc, start_node_id=START, all_slots=SLOTS) == []
    assert run_doc.result.node_vectors is None, "an empty run gained a vector"


def test_a_graph_with_no_start_node_is_left_alone():
    """Every entry carries a provenance tag. With no start node there is nothing to tag with, and
    Part 1's guard would refuse the write - so refuse to compose it."""
    run_doc = a_pre_vector_run(a=a_doc("a"))

    assert seed_baseline_from_output(run_doc, start_node_id="", all_slots=SLOTS) == []
    assert run_doc.result.node_vectors is None


# --------------------------------------------------------------------------- the shape it builds


def test_one_execution_becomes_one_entry_in_every_slot():
    """A node that ran once is case (i): the first execution seeds every dimension (OQ-2)."""
    run_doc = a_pre_vector_run(a=a_doc("a", runs=1))

    assert seed_baseline_from_output(run_doc, start_node_id=START, all_slots=SLOTS) == ["a"]

    assert entries(run_doc, "a", BASELINE_SLOT) == [(START, 0, "a run 0")]
    assert entries(run_doc, "a", "1") == [(START, 0, "a run 0")]


def test_every_recorded_execution_reaches_every_slot():
    """`results` holds only baseline-column executions, and every one of those writes every slot.

    A redirect turn targets `propagation_slots`, which never contains slot 0, so
    `record_node_result` leaves `output[node]` alone and the turn never reaches `results` -
    measured on a node that executed four times, once forward and three times round the loop, and
    holds one result. So what is being reconstructed is the forward pass, continue-runs and chats,
    all of which seed every dimension.

    Seeding turn 0 broadly and later turns narrowly would borrow the redirect-turn rule for
    entries no redirect turn produced, and would leave a re-entering node unable to see a chat.
    """
    run_doc = a_pre_vector_run(a=a_doc("a", runs=3))

    seed_baseline_from_output(run_doc, start_node_id=START, all_slots=SLOTS)

    expected = [(START, 0, "a run 0"), (START, 1, "a run 1"), (START, 2, "a run 2")]
    for slot in SLOTS:
        assert entries(run_doc, "a", slot) == expected, f"slot {slot} is missing later turns"


def test_the_final_entry_comes_from_the_document_not_from_results():
    """`baseline_matches_action_doc` compares the highest-turn entry against the ActionDoc.

    Sourcing that entry from `results[-1]` would make the mirror hold only while those two agree.
    Building it from the document makes it hold by construction. The fixture forces them apart.
    """
    doc = a_doc("a", runs=3, latest="what the document actually holds")
    run_doc = a_pre_vector_run(a=doc)

    seed_baseline_from_output(run_doc, start_node_id=START, all_slots=SLOTS)

    assert entries(run_doc, "a")[-1] == (START, 2, "what the document actually holds")
    assert baseline_matches_action_doc(
        run_doc.result.node_vectors["a"], run_doc.result.output["a"], start_node_id=START
    ), "the seeded baseline does not mirror the document it was built from"


def test_a_node_with_no_recorded_results_still_gets_its_document():
    """`results` can be empty - the node ran but the list was never populated. The document's own
    input and output are still a real execution and must not be dropped."""
    run_doc = a_pre_vector_run(a=a_doc("a", runs=0, latest="only on the document"))

    seed_baseline_from_output(run_doc, start_node_id=START, all_slots=SLOTS)

    assert entries(run_doc, "a") == [(START, 0, "only on the document")]


def test_every_node_in_output_is_seeded():
    run_doc = a_pre_vector_run(s=a_doc("s"), a=a_doc("a"), b=a_doc("b"))

    assert seed_baseline_from_output(run_doc, start_node_id=START, all_slots=SLOTS) == [
        "a",
        "b",
        "s",
    ]


# --------------------------------------------------------------------------- what it buys


def test_a_seeded_node_reads_as_done_instead_of_never_run():
    """The whole point, at the level of the one call that was getting it wrong."""
    run_doc = a_pre_vector_run(a=a_doc("a"))
    # Force the drift reading: a vector map exists, but this node has no entry in it.
    run_doc.result.node_vectors = {}
    assert not is_node_done(run_doc, "a", context_index=BASELINE_SLOT), (
        "the fixture does not reproduce the misreading"
    )

    run_doc.result.node_vectors = None
    seed_baseline_from_output(run_doc, start_node_id=START, all_slots=SLOTS)

    assert is_node_done(run_doc, "a", context_index=BASELINE_SLOT)
    assert is_node_done(run_doc, "a", context_index=BASELINE_SLOT, tag=START, turn_no=0)


@pytest.mark.asyncio
async def test_a_partly_finished_pre_vector_run_does_not_re_execute_on_resume():
    """End to end, and the reason this is not merely tidy.

    A run that parked partway before the change, resumed twice after it. The second resume is
    where it bites: by then some node has written a vector, so `vectors.get(node)` is None for
    every node that finished before the deploy and each reads as never having run.

    Measured without the seed: resume #2 re-executed `s` and `a` five times between them, and the
    cycle itself came out wrong - `z` turned four times with `a` never re-entering.
    """
    graph_doc = a_graph_doc(CHAIN)
    graph_doc.redirect_edges = REDIRECT
    run_doc = a_run_doc(graph_doc)
    service, _ = a_probed_service(run_doc)
    counter = {"n": 0}
    executed: List[str] = []

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        counter["n"] += 1
        node = action_id.removeprefix("action-")
        executed.append(node)
        assert len(executed) <= 200, f"runaway: {executed}"
        return a_doc(node, latest=f"{node}#{counter['n']}")

    async def drive():
        with patch.object(
            ActionGraph, "run_action", AsyncMock(side_effect=fake_run_action)
        ):
            await ActionGraph.run_action_graph_v2(
                ctx=Context(),
                app_state=AppState(),
                user_actions=AsyncMock(),
                action_graph_input_dict={"text": "in"},
                action_graph_result_doc=run_doc,
                action_graph_node_id=None,
                is_continue_run_graph=False,
                user_action_graph_result=service,
                notify=None,
            )

    await drive()

    # Exactly what a run that parked partway under the old code looks like: `s` and `a` finished,
    # `b` and `z` never produced anything, and no vectors at all.
    for never_ran in ("b", "z"):
        run_doc.result.output.pop(never_ran, None)
    run_doc.result.node_vectors = None
    run_doc.result.turn_counters = None
    run_doc.result.open_turns = None
    run_doc.result.redirect_budgets = None

    executed.clear()
    await drive()
    executed.clear()
    await drive()

    assert "s" not in executed and "a" not in executed, (
        f"a node that finished before the change was re-executed and re-billed: {executed}"
    )
