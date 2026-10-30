"""A cyclic graph with no single start node is refused where the reason is still explainable.

Found in review. `start_node_id = topology.start_node_id or ""`, and a graph with two roots has
none - so case (i) built a `NodeRunContext` whose provenance tag was `""`. Part 1's guard caught
it, which is the good news: it refuses to write a mistagged entry rather than silently corrupting
the transcript. But the run then died on an internal invariant message with nothing executed:

    error = "Node 'r1' writes context slots but has no provenance tag. Pass start_node_id for a
             forward pass, or tag=<redirecting node> for a re-entry."
    nodes with output: []

That message is written for whoever is building a `NodeRunContext`, not for someone who drew a
second root in the editor. Both tests below assert on the *message*, because the failure was never
that the run failed - it was that nobody could tell why.
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
from src.autobots.event_result.event_result_model import EventResultStatus
from tests.graphs.persistence_probe import (
    a_graph_doc,
    a_probed_service,
    a_run_doc,
    an_action_result,
)

# Two roots: `s` and `t` both feed `m`, and neither has a parent.
TWO_ROOTS: Dict[str, List[str]] = {"s": ["m"], "t": ["m"], "m": ["z"]}


async def run(graph, redirect_edges):
    graph_doc = a_graph_doc(graph)
    graph_doc.redirect_edges = redirect_edges
    run_doc = a_run_doc(graph_doc)
    service, _ = a_probed_service(run_doc)
    executed: List[str] = []

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        executed.append(action_id.removeprefix("action-"))
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
    return final, executed


def test_the_precondition_is_real_on_this_shape():
    """Guard against the test passing for the wrong reason - two roots, so no start node."""
    graph_doc = a_graph_doc(TWO_ROOTS)
    graph_doc.redirect_edges = [EdgeSpec(source="z", target="m")]
    topology = GraphTopology.build(graph_doc)

    assert topology.K == 2, "the graph has to be cyclic for the precondition to apply"
    assert topology.start_node_id is None
    assert set(topology.roots) == {"s", "t"}


@pytest.mark.asyncio
async def test_a_cyclic_graph_with_two_roots_is_refused_by_name():
    final, executed = await run(TWO_ROOTS, [EdgeSpec(source="z", target="m")])

    assert final.status == EventResultStatus.error
    message = final.error_message.text
    assert "start node" in message, message
    assert "'s'" in message and "'t'" in message, (
        f"the message must name the roots the user drew: {message}"
    )
    assert "provenance tag" not in message, (
        "this is the internal message the guard used to surface; it explains nothing to an author"
    )
    assert executed == [], "nothing should run on a graph that cannot be tagged"


@pytest.mark.asyncio
async def test_the_same_graph_still_runs_as_a_dag():
    """The precondition belongs to `K > 1`, not to graphs in general.

    A DAG writes no vector and needs no tag, so a second root has always been legal for it and
    stays legal. Refusing it here would break existing multi-root graphs for no benefit (RISK-1).
    """
    final, executed = await run(TWO_ROOTS, [])

    assert final.status == EventResultStatus.success
    assert sorted(executed) == ["m", "s", "t", "z"]
    assert final.result.node_vectors is None


@pytest.mark.asyncio
async def test_a_cyclic_graph_with_one_root_is_unaffected():
    final, executed = await run(
        {"s": ["a"], "a": ["b"], "b": ["z"]}, [EdgeSpec(source="z", target="a")]
    )

    assert final.status != EventResultStatus.error, final.error_message
    assert executed.count("a") > 1, "the cycle never ran"
