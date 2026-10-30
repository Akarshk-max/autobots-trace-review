"""Tests for memoisation of the action-graph traversals.

Each of these walks `result.graph` (the data-edge adjacency) with a BFS. Without a visited set a
node reachable by N distinct paths is processed N times, which is exponential in the number of
diamonds - a ladder of k diamonds costs 2^k visits. Two of the traversals do real work per visit
(one appends to a prompt string, one issues a DB lookup), so the re-visits were also producing
duplicated output.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from src.autobots.action_graph.action_graph.action_graph import ActionGraph
from src.autobots.action_graph.action_graph.action_group import ActionGroup
from src.autobots.action_graph.action_graph.coffman_graham import (
    GraphCycleError,
    coffman_graham,
    invert_graph,
)
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context


def diamond_ladder(k: int) -> Dict[str, List[str]]:
    """`k` diamonds in series: a0 -> {b0, c0} -> a1 -> {b1, c1} -> a2 ...

    Un-memoised BFS reaches `a{k}` by 2^k distinct paths.
    """
    graph: Dict[str, List[str]] = {}
    for i in range(k):
        graph[f"a{i}"] = [f"b{i}", f"c{i}"]
        graph[f"b{i}"] = [f"a{i + 1}"]
        graph[f"c{i}"] = [f"a{i + 1}"]
    return graph


def node_ids(graph: Dict[str, List[str]]) -> List[str]:
    nodes = set(graph)
    for children in graph.values():
        nodes.update(children)
    return sorted(nodes)


class CountingGraph(dict):
    """A graph adjacency that records every lookup, so re-expansion is observable.

    Both traversals under test write idempotently, so the *result* is identical with or without
    memoisation - only the number of expansions differs.
    """

    def __init__(self, data: Dict[str, List[str]]):
        super().__init__(data)
        self.lookups: List[str] = []

    def get(self, key, default=None):
        self.lookups.append(key)
        return super().get(key, default)


class FakeNodeData:
    def __init__(self, action_id: str):
        self.actionId = action_id


class FakeNode:
    def __init__(self, action_id: str):
        self.data = FakeNodeData(action_id)


class FakeGraphDoc:
    def __init__(self, graph: Dict[str, List[str]]):
        self.id = "graph-1"
        self.name = "fake graph"
        self.version = 0.0
        self.graph = graph
        self.nodes = {n: f"action-{n}" for n in node_ids(graph)}
        self.node_details = {n: FakeNode(f"action-{n}") for n in node_ids(graph)}
        self.output: Dict[str, Any] = {n: {"blocks": []} for n in node_ids(graph)}
        # The real ActionGraphDoc always has this; without it forget_node() raises and
        # empty_downstream_nodes' broad `except Exception` swallows it, emptying nothing.
        self.node_vectors = None


class FakeResultDoc:
    def __init__(self, graph: Dict[str, List[str]]):
        self.result = FakeGraphDoc(graph)


@pytest.mark.asyncio
async def test_empty_downstream_nodes_visits_each_node_once():
    """A 12-diamond ladder is 2^12 = 4096 un-memoised visits; memoised it is 37 nodes."""
    graph = diamond_ladder(12)
    result_doc = FakeResultDoc(graph)
    counting = CountingGraph(graph)
    result_doc.result.graph = counting

    await ActionGraph.empty_downstream_nodes(
        ctx=Context(), action_graph_node_id="a0", action_graph_result_doc=result_doc
    )

    assert len(counting.lookups) == len(set(counting.lookups)), "a node was expanded twice"
    assert len(counting.lookups) == len(node_ids(graph))


@pytest.mark.asyncio
async def test_empty_downstream_nodes_clears_whole_subtree():
    """Memoisation must not change what gets emptied."""
    graph = diamond_ladder(3)
    result_doc = FakeResultDoc(graph)

    await ActionGraph.empty_downstream_nodes(
        ctx=Context(), action_graph_node_id="a0", action_graph_result_doc=result_doc
    )

    assert result_doc.result.output == {}


@pytest.mark.asyncio
async def test_empty_downstream_only_keeps_the_root():
    graph = diamond_ladder(2)
    result_doc = FakeResultDoc(graph)
    root = "a0"

    await ActionGraph.empty_downstream_nodes(
        ctx=Context(),
        action_graph_node_id=root,
        action_graph_result_doc=result_doc,
        is_empty_downstream_only=True,
    )

    assert list(result_doc.result.output) == [root]


def test_get_subgraph_map_visits_each_node_once():
    graph = diamond_ladder(12)
    counting = CountingGraph(graph)

    subgraph = ActionGraph.get_subgraph_map(ctx=Context(), node_id="a0", graph=counting)

    assert len(counting.lookups) == len(set(counting.lookups)), "a node was expanded twice"
    # Only nodes with children appear as keys; the final `a{k}` is a leaf.
    assert set(subgraph) == {n for n in node_ids(graph) if graph.get(n)}
    for node, children in subgraph.items():
        assert children == graph[node]


def test_get_subgraph_map_from_midpoint():
    graph = diamond_ladder(3)
    subgraph = ActionGraph.get_subgraph_map(ctx=Context(), node_id="a1", graph=graph)

    assert "a0" not in subgraph
    assert "a1" in subgraph and "a2" in subgraph


@pytest.mark.asyncio
async def test_hierarchy_prompt_has_no_duplicate_lines():
    """The un-memoised walk described the same relationship once per path reaching it."""
    graph = diamond_ladder(4)
    result_doc = FakeResultDoc(graph)

    prompt = await ActionGroup.build_node_group_hierarchy_prompt(
        ctx=Context(), action_graph_result_doc=result_doc, node_id="a0"
    )

    lines = [ln for ln in prompt.splitlines() if ln.strip()]
    assert lines, "expected a non-empty hierarchy prompt"
    assert len(lines) == len(set(lines)), "hierarchy prompt repeated a relationship"


@pytest.mark.asyncio
async def test_profile_prompt_issues_one_db_lookup_per_node():
    """`list_owned_or_shared_with` was called once per path, not once per node."""
    graph = diamond_ladder(6)
    result_doc = FakeResultDoc(graph)

    async def fake_list(action_find):
        doc = type("D", (), {"id": action_find.id, "name": "n", "description": "d", "user_manual": "m"})()
        return type("P", (), {"total_count": 1, "docs": [doc]})()

    user_actions = AsyncMock()
    user_actions.list_owned_or_shared_with = AsyncMock(side_effect=fake_list)

    await ActionGroup.build_node_group_profile_prompt(
        ctx=Context(),
        user_actions=user_actions,
        action_graph_result_doc=result_doc,
        node_id="a0",
    )

    # Every node except the root a0, which is the node we are building the prompt for.
    expected = len(node_ids(graph)) - 1
    assert user_actions.list_owned_or_shared_with.await_count == expected


@pytest.mark.asyncio
async def test_profile_prompt_does_not_mutate_the_graph():
    """`to_visit_nodes = child_nodes` aliased the graph's own list, so `+=` corrupted it."""
    graph = diamond_ladder(3)
    result_doc = FakeResultDoc(graph)
    before = {node: list(children) for node, children in graph.items()}

    user_actions = AsyncMock()
    user_actions.list_owned_or_shared_with = AsyncMock(
        side_effect=Exception("not needed for this assertion")
    )

    await ActionGroup.build_node_group_profile_prompt(
        ctx=Context(),
        user_actions=user_actions,
        action_graph_result_doc=result_doc,
        node_id="a0",
    )

    assert graph == before


def test_coffman_graham_raises_on_a_cyclic_graph():
    """Previously the cycle's nodes were silently dropped and the run still reported success."""
    cyclic = {"a": ["b"], "b": ["c"], "c": ["a"]}

    with pytest.raises(GraphCycleError) as exc:
        coffman_graham(cyclic, len(cyclic))

    assert exc.value.http_status == 400
    assert exc.value.unreachable_nodes == {"a", "b", "c"}


def test_coffman_graham_can_stay_warn_only():
    """The engine passes raise_on_cycle from ACTION_GRAPH_VALIDATION_ENFORCED, so the one change
    that turns a (wrongly) succeeding run into a failure flips when validation flips."""
    cyclic = {"a": ["b"], "b": ["c"], "c": ["a"]}

    layers = coffman_graham(cyclic, len(cyclic), raise_on_cycle=False)

    # Historical behaviour preserved: the cycle's nodes are simply dropped from the layering.
    assert [n for layer in layers for n in layer] == []


def test_validation_always_detects_the_cycle_regardless():
    """Validation wants the condition reported even while the engine stays warn-only."""
    from src.autobots.action_graph.action_graph.action_graph_doc_model import ActionGraphDoc
    from src.autobots.action_graph.action_graph.graph_topology import GraphTopology, ValidationCode

    doc = ActionGraphDoc(_id="g", user_id="u", name="n", graph={"s": ["a"], "a": ["b"], "b": ["a"]})

    assert ValidationCode.data_edge_cycle in [i.code for i in GraphTopology.build(doc).validate()]


def test_coffman_graham_reports_only_the_unreachable_nodes():
    graph = {"root": ["a"], "a": ["b"], "b": ["c"], "c": ["b"]}

    with pytest.raises(GraphCycleError) as exc:
        coffman_graham(graph, len(graph))

    assert "root" not in exc.value.unreachable_nodes
    assert {"b", "c"} <= exc.value.unreachable_nodes


def test_coffman_graham_still_layers_a_dag():
    """Regression guard: the cycle check must not disturb the acyclic path.

    Uses `width = len(nodes)`, which is how the engine calls it - the layer-overflow branch can
    never fire there, so layers are pure depth groupings and topological order holds.
    """
    graph = {"A": ["B", "C", "X", "Y"], "B": ["D", "E"], "C": ["F"]}
    inverted = invert_graph(graph)
    layers = coffman_graham(inverted, len(inverted))

    flat = [node for layer in layers for node in layer]
    assert sorted(flat) == sorted(node_ids(graph))
    assert flat.index("A") < flat.index("B")
    assert flat.index("B") < flat.index("D")
    assert flat.index("C") < flat.index("F")


@pytest.mark.asyncio
async def test_a_cyclic_graph_is_marked_errored_once_enforcement_is_on(monkeypatch, set_test_settings):
    """The failure mode this replaces: the cycle's nodes were dropped and the run said `success`.

    The raise also has to happen *inside* `run_action_graph_v2`'s try, or the run is never marked
    errored at all and sits in `processing` until the stuck-graph monitor finds it.
    """
    from src.autobots import SettingsProvider
    from src.autobots.action_graph.action_graph.action_graph_doc_model import (
        ActionGraphDoc,
        Node,
        NodeData,
    )
    from src.autobots.event_result.event_result_model import EventResultStatus

    monkeypatch.setattr(
        SettingsProvider.sget(), "ACTION_GRAPH_VALIDATION_ENFORCED", True, raising=False
    )

    cyclic = {"s": ["a"], "a": ["b"], "b": ["c"], "c": ["a"]}
    graph_doc = ActionGraphDoc(
        id="graph-1",
        user_id="user-1",
        name="cyclic graph",
        nodes={n: f"action-{n}" for n in node_ids(cyclic)},
        node_details={
            n: Node(id=n, data=NodeData(label=n, actionId=f"action-{n}")) for n in node_ids(cyclic)
        },
        graph=cyclic,
        output={},
    )

    result_doc = FakeResultDoc(cyclic)
    result_doc.result = graph_doc
    result_doc.id = "run-1"
    result_doc.user_id = "user-1"

    updates: List[Any] = []

    class FakeResults:
        async def update_action_graph_result(self, ctx, *args, **kwargs):
            updates.append(kwargs.get("action_graph_update") or args[-1])
            return result_doc

        async def save_run_doc(self, ctx, run_doc, **kwargs):
            # The engine's write path since Part 2: it mutates the document in place and saves it,
            # so the status this test asserts on is read off the document rather than a patch.
            updates.append(run_doc)

    await ActionGraph.run_action_graph_v2(
        ctx=Context(), app_state=AppState(), user_actions=None,
        action_graph_input_dict={}, action_graph_result_doc=result_doc,
        action_graph_node_id=None, is_continue_run_graph=False,
        user_action_graph_result=FakeResults(), notify=None,
    )

    statuses = [u.status for u in updates if getattr(u, "status", None)]
    assert EventResultStatus.error in statuses, "a cyclic graph must not be left in `processing`"
    assert EventResultStatus.success not in statuses

    errors = [u.error_message.text for u in updates if getattr(u, "error_message", None)]
    assert any("cycle" in e for e in errors), errors


def test_coffman_graham_width_bound_still_applies():
    """A narrower width bumps nodes into later layers; only completeness is guaranteed."""
    graph = {"A": ["B", "C", "X", "Y"], "B": ["D", "E"], "C": ["F"]}
    layers = coffman_graham(invert_graph(graph), 2)

    flat = [node for layer in layers for node in layer]
    assert sorted(flat) == sorted(node_ids(graph))
    assert all(len(layer) <= 2 for layer in layers)
