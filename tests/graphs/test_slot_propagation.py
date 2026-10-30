"""Which slots a turn writes - Part 4's exit criterion 3.

When redirect *j* ⇢ *i* opens turn *t*, the affected dimensions are the context indices of the
**R.N.s that lie inside `R.S.(i,j)`**, and every node executing in that turn writes **its own**
result into all of them. A **row** update - one node across several of its own dimensions.

**No node ever writes into another node's slot.** The first implementation of this part did exactly
that (`(V_ij)_new` into `V_kj` for every `k ∈ R.S.`), following the plan doc as it then read, and
the result is a node holding its *grandparent's* output - which it should never see. A node learns
what its parent produced by reading that parent's slot, one hop.

**Do not test this on a graph where each redirect's `R.S.` contains only its own R.N.** - the rule
collapses to "write the current slot" there and passes under the wrong version too. §1.6's worked
graph is the one that bites.
"""

from __future__ import annotations

from typing import Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from src.autobots.action_graph.action_graph.action_graph import ActionGraph
from src.autobots.action_graph.action_graph.action_graph_doc_model import EdgeSpec
from src.autobots.action_graph.action_graph.graph_topology import GraphTopology
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from tests.graphs.persistence_probe import (
    a_graph_doc,
    a_probed_service,
    a_run_doc,
    an_action_result,
)

# §1.6's worked graph. R.N. = {2, 5}: node 2 is reached through both 1 and 5, node 5 is a sibling
# of 1. Both redirects target 0.
WORKED = {"-1": ["0"], "0": ["1", "5"], "1": ["2"], "5": ["2"], "2": ["3", "4"]}
BOTH = [EdgeSpec(source="2", target="0"), EdgeSpec(source="5", target="0")]

# A shape where each redirect's R.S. holds only its own R.N., so the write set is a single slot.
# Kept to pin that the narrow case still behaves - not to test the rule.
SIBLINGS = {"S": ["A"], "A": ["B", "E"], "B": ["C"]}
SIBLING_EDGES = [EdgeSpec(source="C", target="A"), EdgeSpec(source="E", target="A")]


def topo(graph: Dict[str, List[str]], edges: List[EdgeSpec]) -> GraphTopology:
    doc = a_graph_doc(graph)
    doc.redirect_edges = edges
    return GraphTopology.build(doc)


# --------------------------------------------------------------------------- the write set


def test_the_write_set_is_the_rns_inside_the_relevant_subgraph():
    """On the worked graph both redirects disturb a region containing both R.N.s, so a turn of
    either writes both dimensions."""
    t = topo(WORKED, BOTH)

    assert t.context_index == {"2": 1, "5": 2}
    assert t.propagation_slots[("0", "2")] == ("1", "2")
    assert t.propagation_slots[("0", "5")] == ("1", "2")


def test_an_rn_outside_the_relevant_subgraph_is_not_written():
    """The negative. `E` is a sibling of `B` - incomparable to `C` - so a C-loop does not disturb
    E's dimension and must not write slot 2."""
    t = topo(SIBLINGS, SIBLING_EDGES)

    assert t.context_index == {"C": 1, "E": 2}
    assert "E" not in t.relevant_subgraph[("A", "C")]
    assert t.propagation_slots[("A", "C")] == ("1",), "an untouched dimension was written"
    assert t.propagation_slots[("A", "E")] == ("2",)


def test_the_redirecting_nodes_own_slot_is_always_in_the_set():
    """`j` is a descendant of itself, so it is always in `R.S.(i,j)` and the set is never empty."""
    for graph, edges in ((WORKED, BOTH), (SIBLINGS, SIBLING_EDGES)):
        t = topo(graph, edges)
        for (i, j), slots in t.propagation_slots.items():
            assert str(t.context_index[j]) in slots, f"{j}'s own slot missing from R.S.({i},{j})"


def test_a_dag_has_no_write_sets():
    assert topo(WORKED, []).propagation_slots == {}


# --------------------------------------------------------------------------- through a real run


async def a_cyclic_run(graph, edges, budget: int = 2):
    graph_doc = a_graph_doc(graph)
    graph_doc.redirect_edges = edges
    run_doc = a_run_doc(graph_doc)
    service, _ = a_probed_service(run_doc)

    async def fake(ctx, app_state, ua, action_id, inp, notify=None):
        return an_action_result(action_id)

    settings = SettingsProvider.sget()
    previous = settings.ACTION_GRAPH_REDIRECT_BUDGET_DEFAULT
    object.__setattr__(settings, "ACTION_GRAPH_REDIRECT_BUDGET_DEFAULT", budget)
    try:
        with patch.object(ActionGraph, "run_action", AsyncMock(side_effect=fake)):
            return await ActionGraph.run_action_graph_v2(
                ctx=Context(), app_state=AppState(), user_actions=AsyncMock(),
                action_graph_input_dict={"text": "in"}, action_graph_result_doc=run_doc,
                action_graph_node_id=None, is_continue_run_graph=False,
                user_action_graph_result=service, notify=None,
            )
    finally:
        object.__setattr__(settings, "ACTION_GRAPH_REDIRECT_BUDGET_DEFAULT", previous)


def tags_in(final, node: str, slot: str) -> List[str]:
    vector = (final.result.node_vectors or {}).get(node)
    return sorted({e.node_id for e in vector.slot(slot).entries}) if vector else []


@pytest.mark.asyncio
async def test_a_turn_reaches_every_dimension_it_disturbs():
    """A 2-loop writes node 1's slot 2 as well as its slot 1, because node 5's dimension is inside
    the region a 2-loop disturbs - so when 5 later loops, its dimension already knows."""
    final = await a_cyclic_run(WORKED, BOTH)

    assert "2" in tags_in(final, "1", "1"), "node 1 recorded no 2-loop turn in slot 1"
    assert "2" in tags_in(final, "1", "2"), (
        "a 2-loop left node 5's dimension of node 1 untouched, so it will read a pre-cycle draft"
    )


@pytest.mark.asyncio
async def test_a_node_holds_only_its_own_work():
    """The defect the first implementation had. Every entry in a node's slot must be that node's
    own execution - never a copy of somebody else's, and never its grandparent's."""
    final = await a_cyclic_run(WORKED, BOTH)

    for node, vector in (final.result.node_vectors or {}).items():
        for key, slot in vector.slots.items():
            for e in slot.entries:
                assert (e.output or {}).get("blocks"), f"{node} slot {key}: empty entry"
                text = e.output["blocks"][0]["text"]
                assert text.endswith(f"out action-{node} 0"), (
                    f"node {node}'s slot {key} holds another node's output: {text!r}"
                )


@pytest.mark.asyncio
async def test_an_untouched_dimension_stays_at_its_baseline():
    """The negative, through the engine. A C-loop must leave E's dimension of `B` holding only the
    forward pass - `B` takes no part in an E-loop and E takes none in a C-loop."""
    final = await a_cyclic_run(SIBLINGS, SIBLING_EDGES)

    assert tags_in(final, "B", "1") == ["C", "S"], "B should record the C-loop in slot 1"
    assert tags_in(final, "B", "2") == ["S"], (
        "an E-loop dimension was written by a C-loop turn"
    )


@pytest.mark.asyncio
async def test_a_dag_writes_no_vector_at_all():
    final = await a_cyclic_run(WORKED, [])

    vectors = final.result.node_vectors or {}
    assert vectors, "a DAG with a start node records a transcript too"
    for node, vector in vectors.items():
        assert sorted(vector.slots) == ["0"], (
            f"{node}: K == 1 has one dimension, so there is nothing for a turn to propagate across"
        )
