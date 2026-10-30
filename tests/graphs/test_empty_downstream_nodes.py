"""Tests for `empty_downstream_nodes` - the rerun path that clears stale results.

The failure this guards against is silent: a rerun that runs against results the previous run left
behind still reports `success`, and nothing in the output says which nodes were recomputed and
which were not. The old implementation wrapped the whole per-node body in `except Exception` with
the `to_visit` extension *inside* it, so a single bad node both swallowed the error and pruned its
entire subtree from the walk.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

import pytest

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action_graph.action_graph.action_graph import ActionGraph
from src.autobots.action_graph.action_graph.action_graph_doc_model import (
    ActionGraphDoc,
    ActionGraphLiteDoc,
    Node,
    NodeData,
)
from src.autobots.action_graph.action_graph.node_result_writer import (
    NodeRunContext,
    record_node_result,
)
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
)
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus, EventType

START = "s"


def an_action_doc(node: str) -> ActionDoc:
    return ActionDoc(
        _id=f"action-{node}",
        user_id="user-1",
        name=f"action {node}",
        type=ActionType.mock_action,
        input={"text": "in"},
        output={"blocks": [{"text": f"stale {node}"}]},
    )


def a_node(node: str, *, action_id: str | None = None) -> Node:
    """A node's details. The mock Start node carries `actionId: ""` by convention."""
    return Node(
        id=node,
        data=NodeData(label=node, actionId=f"action-{node}" if action_id is None else action_id),
    )


def a_run(
    graph: Dict[str, List[str]],
    *,
    executed: List[str],
    node_details: Dict[str, Node] | None = None,
) -> ActionGraphResultDoc:
    """A run with `executed` already holding results, ready to be emptied."""
    details = (
        node_details
        if node_details is not None
        else {n: a_node(n) for n in {START, *graph, *(d for ds in graph.values() for d in ds)}}
    )
    if START in details:
        details[START] = a_node(START, action_id="")

    doc = ActionGraphDoc(
        _id="graph-1",
        user_id="user-1",
        name="g",
        graph=graph,
        node_details=details,
        output={},
    )
    run = ActionGraphResultDoc(
        _id="run-1",
        user_id="user-1",
        type=EventType.action_graph_run,
        status=EventResultStatus.processing,
        result=doc,
    )
    for node in executed:
        record_node_result(run, an_action_doc(node), NodeRunContext.for_dag(node))
    return run


# --------------------------------------------------------------------------- the happy path


@pytest.mark.asyncio
async def test_it_empties_the_node_and_everything_below_it():
    #  s -> a -> b -> c,  and an incomparable branch s -> x
    run = a_run(
        {START: ["a", "x"], "a": ["b"], "b": ["c"]},
        executed=["a", "b", "c", "x"],
    )

    emptied = await ActionGraph.empty_downstream_nodes(
        ctx=Context(), action_graph_node_id="a", action_graph_result_doc=run
    )

    assert emptied == {"a", "b", "c"}
    assert set(run.result.output) == {"x"}, "an incomparable branch keeps its results"


@pytest.mark.asyncio
async def test_downstream_only_keeps_the_node_the_rerun_starts_from():
    run = a_run({START: ["a"], "a": ["b"], "b": ["c"]}, executed=["a", "b", "c"])

    emptied = await ActionGraph.empty_downstream_nodes(
        ctx=Context(),
        action_graph_node_id="a",
        action_graph_result_doc=run,
        is_empty_downstream_only=True,
    )

    assert emptied == {"b", "c"}
    assert set(run.result.output) == {"a"}


@pytest.mark.asyncio
async def test_the_seed_node_is_matched_by_value_not_identity():
    """`node is not action_graph_node_id` worked only because the seed is the same object.

    Passing an equal-but-distinct string is the same request as far as the caller is concerned, and
    identity comparison would empty the very node the caller asked to keep.
    """
    run = a_run({START: ["a"], "a": ["b"]}, executed=["a", "b"])

    emptied = await ActionGraph.empty_downstream_nodes(
        ctx=Context(),
        action_graph_node_id="".join(["a"]),  # equal to "a", not the interned literal
        action_graph_result_doc=run,
        is_empty_downstream_only=True,
    )

    assert emptied == {"b"}
    assert set(run.result.output) == {"a"}


@pytest.mark.asyncio
async def test_the_start_node_is_never_emptied():
    """The mock Start node has `actionId: ""` and no result to drop."""
    run = a_run({START: ["a"]}, executed=["a"])

    emptied = await ActionGraph.empty_downstream_nodes(
        ctx=Context(), action_graph_node_id=START, action_graph_result_doc=run
    )

    assert emptied == {"a"}


@pytest.mark.asyncio
async def test_a_diamond_visits_each_node_once():
    """`visited` is what keeps this linear; without it a k-diamond ladder is 2^k re-expansions."""
    run = a_run(
        {START: ["a"], "a": ["b", "c"], "b": ["d"], "c": ["d"], "d": ["e"]},
        executed=["a", "b", "c", "d", "e"],
    )

    visits: List[str] = []
    real_graph = run.result.graph

    class CountingGraph(dict):
        def get(self, key, default=None):
            visits.append(key)
            return real_graph.get(key, default)

    run.result.graph = CountingGraph(real_graph)

    await ActionGraph.empty_downstream_nodes(
        ctx=Context(), action_graph_node_id="a", action_graph_result_doc=run
    )

    assert visits == sorted(set(visits), key=visits.index)
    assert sorted(visits) == ["a", "b", "c", "d", "e"]


# --------------------------------------------------------------------------- the regression


@pytest.mark.asyncio
async def test_a_node_without_details_does_not_prune_its_subtree():
    """The bug the broad handler hid.

    `node_details.get(node)` returning None raised `AttributeError` on the next line, which the
    old `except Exception` caught - *inside* the loop, and above `to_visit += downstream_nodes`.
    So `b` was skipped and `c` and `d` below it silently kept their stale results, while the run
    reported success.
    """
    graph = {START: ["a"], "a": ["b"], "b": ["c"], "c": ["d"]}
    details = {n: a_node(n) for n in [START, "a", "c", "d"]}  # "b" deliberately absent

    run = a_run(graph, executed=["a", "b", "c", "d"], node_details=details)

    emptied = await ActionGraph.empty_downstream_nodes(
        ctx=Context(), action_graph_node_id="a", action_graph_result_doc=run
    )

    assert emptied == {"a", "c", "d"}, "the walk continued past the node with no details"
    assert set(run.result.output) == {"b"}, "only the node that has no details is left"


@pytest.mark.asyncio
async def test_a_real_error_is_raised_rather_than_logged_as_emptied_nothing():
    """A lite result doc reaching `forget_node` is a bug, not a no-op.

    Part 1 gave the writer an explicit precondition for exactly this; the old handler turned that
    `TypeError` back into a swallowed log line, and the rerun then ran against stale results.
    """
    run = a_run({START: ["a"], "a": ["b"]}, executed=["a", "b"])
    run.result = ActionGraphLiteDoc(
        _id="graph-1",
        name="g",
        user_id="user-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    # A lite doc carries no `graph`, so drive the walk through an explicit adjacency instead.
    object.__setattr__(run.result, "graph", {START: ["a"], "a": ["b"]})
    object.__setattr__(run.result, "node_details", {n: a_node(n) for n in [START, "a", "b"]})

    with pytest.raises(TypeError, match="lite result"):
        await ActionGraph.empty_downstream_nodes(
            ctx=Context(), action_graph_node_id="a", action_graph_result_doc=run
        )


@pytest.mark.asyncio
async def test_a_missing_graph_is_not_an_error():
    """A single-node run has no adjacency at all. Nothing downstream is not a failure."""
    run = a_run({}, executed=["a"], node_details={"a": a_node("a")})
    run.result.graph = None

    emptied = await ActionGraph.empty_downstream_nodes(
        ctx=Context(), action_graph_node_id="a", action_graph_result_doc=run
    )

    assert emptied == {"a"}
    assert run.result.output == {}


# --------------------------------------------------------------------------- the caller

@pytest.mark.asyncio
async def test_an_escaping_error_marks_the_run_failed_rather_than_leaving_it_processing():
    """Half of the fix. Raising is only an improvement if the user finds out.

    `_run_as_background_task` used to log and return, leaving `status: processing` until the
    720-minute stuck monitor - indistinguishable from a run that is still working.
    """
    from unittest.mock import AsyncMock, patch

    run = a_run({START: ["a"]}, executed=["a"])
    updates: List[Any] = []

    user_action_graph_result = AsyncMock()

    async def capture(ctx, result_id, update):
        updates.append(update)
        return run

    user_action_graph_result.update_action_graph_result.side_effect = capture

    with patch.object(
        ActionGraph,
        "_run_retry_as_background_task",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await ActionGraph._run_as_background_task(
            ctx=Context(),
            app_state=None,
            user_actions=AsyncMock(),
            action_graph_input_dict={},
            action_graph_result_doc=run,
            user_action_graph_result=user_action_graph_result,
            notify=AsyncMock(),
        )

    assert len(updates) == 1
    assert updates[0].status == EventResultStatus.error
    assert "boom" in updates[0].error_message.text
