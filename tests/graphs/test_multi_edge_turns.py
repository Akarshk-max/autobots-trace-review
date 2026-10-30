"""Several redirect edges leaving one node - the shape Part 3 assumed away and Part 5 makes normal.

The measurement these tests are written from, on `s -> a -> b -> z` with `z ⇢ a` and `z ⇢ b`:

    fired:    {a|z: 3, b|z: 3}                        = 6 turns opened
    z slot 1: [(s,0), (z,0), (z,1), (z,2), (z,3)]     = 4 turns recorded

**Six turns ran. Four survived.** Two turns were handed a turn number that was already in use;
`upsert_entry` correctly treats a matching `(turn_no, node_id)` as one cell and replaces, so the
later turn erased the earlier one. There is a second symptom in the same numbers - a turn whose
identity was already recorded also reads as *already done*, so it is skipped rather than run.

Both are asserted below, and both are counted from the run's own budget totals rather than from a
hard-coded 6, so the tests keep meaning if the default budget changes.
"""

from __future__ import annotations

from typing import Dict, List, Tuple
from unittest.mock import AsyncMock, patch

import pytest

from src.autobots.action_graph.action_graph.action_graph import ActionGraph
from src.autobots.action_graph.action_graph.action_graph_doc_model import EdgeSpec
from src.autobots.action_graph.action_graph.graph_topology import GraphTopology
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from tests.graphs.persistence_probe import (
    a_graph_doc,
    a_probed_service,
    a_run_doc,
    an_action_result,
)
from tests.graphs.test_cyclic_execution import run_graph

CHAIN = {"s": ["a"], "a": ["b"], "b": ["z"]}
TWO_EDGES = [EdgeSpec(source="z", target="a"), EdgeSpec(source="z", target="b")]


def turns_opened_per_column(final) -> Dict[str, int]:
    """How many turns each redirecting node opened, summed over the edges leaving it.

    The budgets are the record of what actually fired, so this is the count the transcript has to
    match. Summing over edges is the point: they share one column (§1.3).
    """
    opened: Dict[str, int] = {}
    for key, budget in (final.result.redirect_budgets or {}).items():
        _target, _, source = key.partition("|")
        opened[source] = opened.get(source, 0) + budget.fired
    return opened


def recorded_turns(final, node: str, context_index: str, tag: str) -> List[int]:
    entries = final.result.node_vectors[node].slot(context_index).tagged(tag)
    return sorted(e.turn_no for e in entries)


# --------------------------------------------------------------------------- the reproduction


@pytest.mark.asyncio
async def test_two_redirect_edges_record_every_turn_they_open():
    """4-of-6 becomes 6-of-6, asserted on the stored document."""
    final, _, probe = await run_graph(CHAIN, redirect_edges=TWO_EDGES)

    opened = turns_opened_per_column(final)["z"]
    turns = recorded_turns(final, "z", "1", "z")

    assert opened > 1, "the shape under test needs several turns to have fired"
    assert len(turns) == opened, f"{opened} turns opened, {len(turns)} recorded: {turns}"
    assert turns == sorted(set(turns)), f"turns collided or went backwards: {turns}"

    import json

    stored = json.loads(probe.stored_document("run-1"))
    persisted = [
        e["turn_no"]
        for e in stored["result"]["node_vectors"]["z"]["slots"]["1"]["entries"]
        if e["node_id"] == "z"
    ]
    assert sorted(persisted) == turns, "the document on disk disagrees with the document in memory"


@pytest.mark.asyncio
async def test_two_redirect_edges_execute_every_turn_they_open():
    """The second symptom: a duplicate `(turn_no, tag)` also reads as *already done*.

    `is_work_done` identifies an execution by that pair, so a turn handed a number already in the
    transcript is skipped rather than run - the collision costs the work as well as the record.
    """
    final, executed, _ = await run_graph(CHAIN, redirect_edges=TWO_EDGES)

    opened = turns_opened_per_column(final)["z"]

    assert executed.count("z") == opened + 1, (
        f"z ran {executed.count('z')} times for {opened} turns plus the forward pass: {executed}"
    )


@pytest.mark.asyncio
async def test_the_two_edges_share_one_column():
    """§2.6: one slot per R.N., not per edge. The turns inside it are what got distinguished."""
    final, _, _ = await run_graph(CHAIN, redirect_edges=TWO_EDGES)

    assert set(final.result.node_vectors["z"].slots) == {"0", "1"}, "K = 2: z is the only R.N."
    assert set(final.result.turn_counters) == {"z|1"}, "one counter for the column"


# --------------------------------------------------------------------------- the property


SHAPES: List[Tuple[str, Dict[str, List[str]], List[EdgeSpec]]] = [
    (
        "one edge",
        CHAIN,
        [EdgeSpec(source="z", target="a")],
    ),
    (
        "two edges from one node",
        CHAIN,
        TWO_EDGES,
    ),
    (
        "three edges from one node",
        {"s": ["a"], "a": ["b"], "b": ["c"], "c": ["z"]},
        [
            EdgeSpec(source="z", target="a"),
            EdgeSpec(source="z", target="b"),
            EdgeSpec(source="z", target="c"),
        ],
    ),
    (
        "two redirecting nodes, two edges each",
        {"s": ["a"], "a": ["b"], "b": ["c"], "c": ["z"]},
        [
            EdgeSpec(source="c", target="a"),
            EdgeSpec(source="c", target="b"),
            EdgeSpec(source="z", target="a"),
            EdgeSpec(source="z", target="c"),
        ],
    ),
    (
        "a redirect into the start node",
        {"s": ["a"], "a": ["b"], "b": ["z"]},
        [EdgeSpec(source="z", target="s"), EdgeSpec(source="z", target="b")],
    ),
]


@pytest.mark.parametrize(
    "shape,graph,redirect_edges", SHAPES, ids=[shape for shape, _, _ in SHAPES]
)
@pytest.mark.asyncio
async def test_every_turn_opened_is_a_turn_recorded(shape, graph, redirect_edges):
    """Across edge counts and redirector counts: nothing opened is lost, and turns stay dense.

    Written as a sweep rather than one hand-picked graph because the failure only appears with two
    turns in flight at once - a single-edge graph passes under the broken version too, which is why
    it went unnoticed until a second edge was drawn.
    """
    final, _, _ = await run_graph(graph, redirect_edges=redirect_edges, max_executions=400)

    topology = GraphTopology.build(final.result)
    opened = turns_opened_per_column(final)

    for redirecting_node, count in opened.items():
        if count == 0:
            continue
        slot = str(topology.context_index[redirecting_node])
        turns = recorded_turns(final, redirecting_node, slot, redirecting_node)
        assert len(turns) == count, (
            f"{shape}: {redirecting_node} opened {count} turns in slot {slot} but recorded {turns}"
        )
        assert turns == sorted(set(turns)), (
            f"{shape}: {redirecting_node}'s turns collided or went backwards: {turns}"
        )


@pytest.mark.parametrize(
    "shape,graph,redirect_edges", SHAPES, ids=[shape for shape, _, _ in SHAPES]
)
@pytest.mark.asyncio
async def test_no_two_entries_anywhere_share_an_identity(shape, graph, redirect_edges):
    """`f_comb`'s identity is `(turn_no, node_id)` within a slot. It must stay unique.

    A slot cannot hold a duplicate - `upsert_entry` replaces on that identity, which is exactly how
    the lost turns disappeared silently. So this counts entries against the identities they carry:
    equal counts mean nothing was quietly replaced.
    """
    final, _, _ = await run_graph(graph, redirect_edges=redirect_edges, max_executions=400)

    for node, vector in (final.result.node_vectors or {}).items():
        for slot_key, slot in vector.slots.items():
            identities = [e.identity() for e in slot.entries]
            assert len(identities) == len(set(identities)), (
                f"{shape}: {node} slot {slot_key} holds a duplicate identity: {identities}"
            )


# --------------------------------------------------------------------------- retry and resume


async def a_run_that_can_be_repeated(graph, redirect_edges):
    """A run doc plus a callable that executes it again against the same document."""
    graph_doc = a_graph_doc(graph)
    graph_doc.redirect_edges = redirect_edges
    run_doc = a_run_doc(graph_doc)
    service, probe = a_probed_service(run_doc)
    executed: List[str] = []

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        executed.append(action_id.removeprefix("action-"))
        assert len(executed) <= 400, f"runaway cycle: {executed}"
        return an_action_result(action_id)

    async def attempt():
        with patch.object(ActionGraph, "run_action", AsyncMock(side_effect=fake_run_action)):
            return await ActionGraph.run_action_graph_v2(
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

    return run_doc, attempt, executed, probe


@pytest.mark.asyncio
async def test_a_retry_does_not_restart_the_numbering():
    """RISK-4, for turn numbers rather than budgets.

    An in-memory counter resets under each of the three stacked retry layers, hands out 0 again,
    and the second attempt overwrites the transcript the first one wrote.
    """
    run_doc, attempt, _, _ = await a_run_that_can_be_repeated(CHAIN, TWO_EDGES)

    final = await attempt()
    counters_after_first = dict(final.result.turn_counters)
    turns_after_first = recorded_turns(final, "z", "1", "z")

    final = await attempt()

    assert final.result.turn_counters == counters_after_first, (
        "the retry moved the counter - the budgets should have stopped every edge from re-firing"
    )
    assert recorded_turns(final, "z", "1", "z") == turns_after_first, (
        "the retry rewrote the transcript instead of recognising it"
    )


@pytest.mark.asyncio
async def test_a_resume_continues_the_numbering_and_grows_the_transcript():
    """The D5 path: raise an exhausted pair's limit, re-run the same result id."""
    run_doc, attempt, _, _ = await a_run_that_can_be_repeated(CHAIN, TWO_EDGES)

    final = await attempt()
    counter_before = final.result.turn_counters["z|1"]
    turns_before = recorded_turns(final, "z", "1", "z")

    for key, budget in list(final.result.redirect_budgets.items()):
        final.result.redirect_budgets[key] = budget.model_copy(
            update={"limit": budget.limit + 1}
        )
    final = await attempt()

    turns_after = recorded_turns(final, "z", "1", "z")
    assert final.result.turn_counters["z|1"] > counter_before, "the numbering restarted"
    assert turns_after[: len(turns_before)] == turns_before, (
        "the resumed turns overwrote the transcript instead of extending it"
    )
    assert turns_after == sorted(set(turns_after)), (
        f"the resumed turns duplicated a number or went backwards: {turns_after}"
    )


@pytest.mark.asyncio
async def test_a_run_from_before_the_counter_existed_seeds_from_its_transcript():
    """A run started under Part 3 has a transcript and no `turn_counters`.

    Its recorded entries are then the only record of how many turns it opened, so they seed the
    machine. Starting at 0 would hand out numbers the transcript already holds and overwrite them -
    which is what `_laps_opened` was reaching for, and the reason it is demoted rather than deleted.
    """
    run_doc, attempt, _, _ = await a_run_that_can_be_repeated(CHAIN, TWO_EDGES)

    final = await attempt()
    turns_before = recorded_turns(final, "z", "1", "z")
    assert turns_before, "the first pass recorded no turn, so there is nothing to resume from"

    # Exactly what a Part 3 document looks like: turns recorded, no counter.
    final.result.turn_counters = None
    for key, budget in list(final.result.redirect_budgets.items()):
        final.result.redirect_budgets[key] = budget.model_copy(
            update={"limit": budget.limit + 1}
        )
    final = await attempt()

    turns_after = recorded_turns(final, "z", "1", "z")
    assert turns_after[: len(turns_before)] == turns_before, (
        "the seeded counter overwrote the transcript it was supposed to continue"
    )
    assert len(turns_after) > len(turns_before), "the resumed run recorded no further turn"
    assert turns_after == sorted(set(turns_after)), (
        f"the seeded turns duplicated a number or went backwards: {turns_after}"
    )
