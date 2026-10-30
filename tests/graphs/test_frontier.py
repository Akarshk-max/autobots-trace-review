"""The frontier scheduler - the work queue that replaced the layer walk.

The property that matters most is negative: for a graph with no redirect edges, the frontier must
drain in *exactly* the order the nested `for layer / for node` loops produced. If that slips, every
workflow in production changes execution order, not just cyclic ones.
"""

from __future__ import annotations

from typing import List
from unittest.mock import AsyncMock, patch

import pytest

from src.autobots.action_graph.action_graph.action_graph import ActionGraph
from src.autobots.action_graph.action_graph.coffman_graham import coffman_graham
from src.autobots.action_graph.action_graph.frontier import FrontierScheduler
from src.autobots.action_graph.action_graph.redirect_stack import EMPTY
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from tests.graphs.persistence_probe import (
    a_graph_doc,
    a_probed_service,
    a_run_doc,
    an_action_result,
)

LAYERS = [["s"], ["a", "b"], ["c"]]


# --------------------------------------------------------------------------- order parity


def test_lap_zero_drains_in_exactly_the_layer_walk_order():
    """The old loop was `for layer in layers: for node in layer`. This must equal it."""
    old_order = [node for layer in LAYERS for node in layer]

    frontier = FrontierScheduler(LAYERS)
    new_order = [scheduled.node for scheduled in frontier]

    assert new_order == old_order


@pytest.mark.parametrize(
    "graph",
    [
        {"s": ["a"], "a": ["b"], "b": ["c"]},
        {"s": ["a", "b"], "a": ["c"], "b": ["c"], "c": ["d"]},
        {"s": ["a", "b", "x"], "a": ["c"], "b": ["c"], "x": ["y"], "c": ["d"], "y": ["d"]},
    ],
)
def test_order_parity_holds_for_real_graph_shapes(graph):
    """Driven through the same `coffman_graham` call the engine makes."""
    inverted = {n: [] for n in {*graph, *(d for ds in graph.values() for d in ds)}}
    for source, targets in graph.items():
        for target in targets:
            inverted[target].append(source)

    layers = coffman_graham(inverted, len(inverted))
    expected = [node for layer in layers for node in layer]

    assert [s.node for s in FrontierScheduler(layers)] == expected


def test_every_node_is_scheduled_exactly_once_on_the_forward_pass():
    frontier = FrontierScheduler(LAYERS)
    drained = [s.node for s in frontier]

    assert sorted(drained) == sorted(n for layer in LAYERS for n in layer)
    assert len(drained) == len(set(drained))


def test_a_node_repeated_across_layers_keeps_its_first_position():
    """Rank must be a function of the node, not of whichever copy was seen last."""
    frontier = FrontierScheduler([["s"], ["a"], ["a", "b"]])

    assert [s.node for s in frontier] == ["s", "a", "b"]


def test_the_forward_pass_carries_no_open_redirect():
    frontier = FrontierScheduler(LAYERS)

    for scheduled in frontier:
        assert scheduled.lap == 0
        assert scheduled.stack is EMPTY
        assert scheduled.context_index == "0", "the baseline"
        assert scheduled.tag is None, "provenance on the forward pass is the start node"


def test_the_layer_is_reported_so_the_log_line_survives():
    """The engine logs once per layer; losing that would change what operators grep for."""
    frontier = FrontierScheduler(LAYERS)
    assert [(s.node, s.layer) for s in frontier] == [
        ("s", 0), ("a", 1), ("b", 1), ("c", 2)
    ]


def test_an_empty_layering_schedules_nothing():
    assert not FrontierScheduler([]).has_work
    assert FrontierScheduler([]).pop() is None


# --------------------------------------------------------------------------- re-entry


def test_rearm_queues_a_second_lap_behind_the_first():
    """Wave semantics: a lap completes before the next begins (§1.8.2, Lemma 2)."""
    frontier = FrontierScheduler(LAYERS)
    frontier.pop()  # 's' runs and fires a redirect

    frontier.rearm(["c", "a"], lap=1, stack=EMPTY.push("c", "1", target="a"))

    assert [s.node for s in frontier] == ["a", "b", "c", "a", "c"]


def test_a_rearmed_lap_runs_in_topological_order():
    """The re-run set is queued in rank order however the caller listed it."""
    frontier = FrontierScheduler(LAYERS)
    list(iter(FrontierScheduler(LAYERS)))  # drain a throwaway to be explicit about independence

    queued = frontier.rearm(["c", "s", "b"], lap=1, stack=EMPTY.push("c", "1", target="a"))

    assert queued == ("s", "b", "c")


def test_a_rearmed_node_carries_the_stack_it_was_armed_with():
    frontier = FrontierScheduler(LAYERS)
    while frontier.pop() is not None:
        pass
    stack = EMPTY.push("c", "1", target="a")

    frontier.rearm(["a"], lap=1, stack=stack)
    scheduled = frontier.pop()

    assert scheduled.lap == 1
    assert scheduled.stack == stack
    assert scheduled.context_index == "1"
    assert scheduled.tag == "c", "provenance is the redirecting node"
    assert scheduled.redirect_target == "a", "and the edge it came back along"


def test_the_forward_pass_has_no_redirect_target():
    """Lap 0 was not opened by an edge, so there is nothing to name."""
    frontier = FrontierScheduler(LAYERS)

    assert frontier.pop().redirect_target is None


def test_rearm_deduplicates():
    frontier = FrontierScheduler(LAYERS)
    while frontier.pop() is not None:
        pass

    frontier.rearm(["a", "a", "a"], lap=1)

    assert len([s.node for s in frontier]) == 1


def test_an_unknown_node_sorts_last_rather_than_raising():
    """A redirect target the layering never saw is a scheduling question, not a crash."""
    frontier = FrontierScheduler(LAYERS)
    while frontier.pop() is not None:
        pass

    assert frontier.rearm(["zzz", "a"], lap=1) == ("a", "zzz")


def test_a_dag_never_reaches_a_second_lap():
    """Nothing calls `rearm` without a redirect edge, so the queue drains once."""
    frontier = FrontierScheduler(LAYERS)

    assert all(s.lap == 0 for s in frontier)


# --------------------------------------------------------------------------- through the engine


@pytest.mark.asyncio
async def test_the_engine_still_runs_every_node_of_a_dag_in_layer_order():
    """End to end: the swap must not change which nodes run, or in what order."""
    graph = {"s": ["a", "b"], "a": ["c"], "b": ["c"]}
    run_doc = a_run_doc(a_graph_doc(graph))
    service, _ = a_probed_service(run_doc)

    executed: List[str] = []

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        executed.append(action_id)
        return an_action_result(action_id)

    with patch.object(ActionGraph, "run_action", AsyncMock(side_effect=fake_run_action)):
        final = await ActionGraph.run_action_graph_v2(
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

    assert executed == ["action-s", "action-a", "action-b", "action-c"]
    assert set(final.result.output) == {"s", "a", "b", "c"}
    # A DAG keeps a vector now, but only one slot of it. Order and membership above are the
    # properties this test exists for and neither moved.
    assert sorted((final.result.node_vectors or {})["s"].slots) == ["0"]
