"""A crash mid-turn completes that turn on the retry rather than abandoning it (RISK-12).

**These were `xfail(strict=True)` until Part 4.** They asserted the behaviour we wanted while the
engine did the other thing, so they failed the build the moment it was fixed - which is how the
fix announced itself. The characterisation test that recorded the old behaviour was deleted at the
same time, as its docstring instructed.

### What used to happen

The turn counter was durable. The **open frames** were not - nothing recorded that a turn had been
handed out and never completed. On a retry the frontier is rebuilt empty, every node reads as done
for the forward pass, and the redirecting node fires again taking the *next* number.

    attempt 1 (crash during turn 0)   a:[0]      b:[0]      c:[]     d:[]
    attempt 2 (the retry)             a:[0,1,2]  b:[0,1,2]  c:[1,2]  d:[1,2]
                                                            ^^^^^^ c and d never got turn 0

Two costs. The transcript held a turn that only half the re-run set took part in - harmless only
while nothing read a slot, which is exactly what Part 4 changed. And the abandoned turn still
charged the budget, so a crash permanently cost the user one of their turns.

### The fix, and the one it is not

`open_turns` records `turn -> the edge that opened it`. On startup any turn whose relevant
subgraph has not fully recorded it re-arms the nodes that still owe it, with the original frame
reconstructed from the key. The budget is not charged again.

**Complete the turn; do not roll it back.** The nodes that did record it really executed and
really cost money, and `merge_slots` is explicitly "pure addition - nothing is ever overwritten and
nothing is dropped". Rollback would also need to know which nodes owed the turn, which needs the
edge - the very thing that was not saved.

### Note

**Turn numbers do not need to be dense.** Identity is exact-match `(turn_no, tag)` and ordering
only needs them to sort, so an abandoned turn leaving a hole in the sequence is harmless.
`test_multi_edge_turns.py` used to assert `turns == list(range(n))`, which was really asserting "no crash
happened" rather than an invariant - do not treat it as one.
"""

from __future__ import annotations

from typing import Dict, List
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

CHAIN = {"a": ["b"], "b": ["c"], "c": ["d"]}
ONE_EDGE = [EdgeSpec(source="d", target="a")]

# The forward pass is 4 executions (a, b, c, d); turn 0 then re-runs a, b, c, d. Crashing on the
# 7th lands in the middle of turn 0, after a and b have recorded it and before c and d have.
CRASH_ON_EXECUTION = 7


async def a_run_that_crashes_once(crash_on: int):
    """Run the graph twice against one document, failing the first attempt mid-turn."""
    graph_doc = a_graph_doc(CHAIN)
    graph_doc.redirect_edges = ONE_EDGE
    run_doc = a_run_doc(graph_doc)
    service, _ = a_probed_service(run_doc)

    executed: List[str] = []
    fail_at = [crash_on]

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        executed.append(action_id.removeprefix("action-"))
        if len(executed) == fail_at[0]:
            raise RuntimeError("simulated crash mid-turn")
        assert len(executed) <= 400, f"runaway: {executed}"
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

    crashed = await attempt()
    after_crash = turns_per_node(crashed)

    fail_at[0] = -1  # the retry runs cleanly
    executed.clear()
    retried = await attempt()
    return after_crash, retried, executed


def turns_per_node(final, tag: str = "d") -> Dict[str, List[int]]:
    return {
        node: sorted(e.turn_no for e in vector.slot("1").tagged(tag))
        for node, vector in sorted((final.result.node_vectors or {}).items())
    }


# --------------------------------------------------------------------------- the fix


@pytest.mark.asyncio
async def test_a_retry_completes_the_turn_the_crash_interrupted():
    """Every node in the turn's re-run set should end up holding that turn."""
    _, retried, _ = await a_run_that_crashes_once(CRASH_ON_EXECUTION)

    topology = GraphTopology.build(retried.result)
    taking_part = topology.relevant_subgraph[("a", "d")]
    turns = turns_per_node(retried)

    # The interrupted turn is the LOWEST one this redirect opened, not necessarily 0: turn numbers
    # are drawn from a run-wide floor and the forward pass owns 0, so `d`'s first turn is 1.
    every = sorted({turn for recorded in turns.values() for turn in recorded})
    assert every, "the retry recorded no turn at all, so there is nothing to check"
    interrupted = every[0]

    missing = {
        node: turns.get(node, [])
        for node in sorted(taking_part)
        if interrupted not in turns.get(node, [])
    }
    assert not missing, (
        f"these nodes never received the interrupted turn {interrupted}: {missing}"
    )


@pytest.mark.asyncio
async def test_a_crash_does_not_cost_the_user_a_turn_of_budget():
    """Budget pays for turns that happen. An abandoned one should not be charged."""
    _, retried, _ = await a_run_that_crashes_once(CRASH_ON_EXECUTION)

    fired = retried.result.redirect_budgets["a|d"].fired
    per_node = turns_per_node(retried)
    # The turns that actually exist, rather than `range(fired)` - numbering is run-wide now, so a
    # column's turns are not 0..n-1.
    every = sorted({turn for recorded in per_node.values() for turn in recorded})
    complete = sum(
        1 for turn in every if all(turn in turns for turns in per_node.values())
    )
    assert complete == fired, (
        f"{fired} turns charged to the budget but only {complete} completed"
    )


# --------------------------------------------------------------------------- the record itself


@pytest.mark.asyncio
async def test_a_dag_never_gains_the_outstanding_turn_record():
    """K == 1 opens no turns, so the key must stay absent - `None`, never `{}`.

    A `{}` default would be written on every partial save and make a DAG's run document differ
    from one produced before cyclic support existed.
    """
    graph_doc = a_graph_doc(CHAIN)  # no redirect edges
    run_doc = a_run_doc(graph_doc)
    service, _ = a_probed_service(run_doc)

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        return an_action_result(action_id)

    with patch.object(ActionGraph, "run_action", AsyncMock(side_effect=fake_run_action)):
        final = await ActionGraph.run_action_graph_v2(
            ctx=Context(), app_state=AppState(), user_actions=AsyncMock(),
            action_graph_input_dict={"text": "in"}, action_graph_result_doc=run_doc,
            action_graph_node_id=None, is_continue_run_graph=False,
            user_action_graph_result=service, notify=None,
        )

    assert final.result.open_turns is None


@pytest.mark.asyncio
async def test_a_run_that_completes_its_turns_leaves_nothing_outstanding():
    """The record is pruned to what is genuinely outstanding, so it does not grow with every
    turn a healthy run completed - and a later resume re-arms only real work."""
    _, retried, _ = await a_run_that_crashes_once(CRASH_ON_EXECUTION)

    assert retried.result.open_turns is None, (
        f"turns still recorded as outstanding after a clean finish: {retried.result.open_turns}"
    )


@pytest.mark.asyncio
async def test_resuming_a_completed_turn_does_not_re_execute_it():
    """A third attempt has nothing to complete, so it must not re-run the turn's nodes again.

    Identity is `(turn_no, tag)`: a node that recorded the turn reads as done for it, so re-arming
    is idempotent even if the record were stale.
    """
    _, retried, _ = await a_run_that_crashes_once(CRASH_ON_EXECUTION)
    before = turns_per_node(retried)
    fired_before = retried.result.redirect_budgets["a|d"].fired

    service, _ = a_probed_service(retried)

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        return an_action_result(action_id)

    with patch.object(ActionGraph, "run_action", AsyncMock(side_effect=fake_run_action)):
        third = await ActionGraph.run_action_graph_v2(
            ctx=Context(), app_state=AppState(), user_actions=AsyncMock(),
            action_graph_input_dict={"text": "in"}, action_graph_result_doc=retried,
            action_graph_node_id=None, is_continue_run_graph=False,
            user_action_graph_result=service, notify=None,
        )

    assert turns_per_node(third) == before
    assert third.result.redirect_budgets["a|d"].fired == fired_before
