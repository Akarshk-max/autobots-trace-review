"""Tests for GraphTopology - the precomputed structural facts about an action graph.

The worked example throughout is the one from the design notes:

    -1 -> 0 -> 1 -> 2 -> 3
          |         |`-> 4
          `--> 5 ---'
    redirects: 2 -> 0, 5 -> 0
"""

from __future__ import annotations

from typing import Any, Dict, List

from src.autobots.action_graph.action_graph.action_graph_doc_model import (
    ActionGraphDoc,
    EdgeSpec,
)
from src.autobots.action_graph.action_graph.graph_topology import (
    BASELINE_CONTEXT_INDEX,
    GraphTopology,
    ValidationCode,
)

WORKED_GRAPH = {
    "-1": ["0"],
    "0": ["1", "5"],
    "1": ["2"],
    "5": ["2"],
    "2": ["3", "4"],
}


def a_doc(**overrides: Any) -> ActionGraphDoc:
    payload: Dict[str, Any] = {
        "_id": "graph-1",
        "user_id": "user-1",
        "name": "g",
        "graph": WORKED_GRAPH,
    }
    payload.update(overrides)
    return ActionGraphDoc(**payload)


def worked_topology() -> GraphTopology:
    return GraphTopology.build(
        a_doc(
            redirect_edges=[
                {"source": "2", "target": "0"},
                {"source": "5", "target": "0"},
            ]
        )
    )


def codes(issues) -> List[ValidationCode]:
    return [i.code for i in issues]


# --------------------------------------------------------------------------- shape


def test_adjacency_and_reverse():
    topo = GraphTopology.build(a_doc())

    assert topo.nodes == frozenset({"-1", "0", "1", "2", "3", "4", "5"})
    assert topo.data_adj["0"] == ("1", "5")
    assert topo.data_rev["2"] == ("1", "5")
    assert topo.data_rev["-1"] == ()


def test_ancestors_and_descendants_are_inclusive():
    topo = GraphTopology.build(a_doc())

    assert topo.descendants["0"] == frozenset({"0", "1", "5", "2", "3", "4"})
    assert topo.ancestors["2"] == frozenset({"2", "1", "5", "0", "-1"})
    assert topo.descendants["3"] == frozenset({"3"})


def test_start_node_is_the_unique_root():
    assert GraphTopology.build(a_doc()).start_node_id == "-1"


# --------------------------------------------------------------------------- R.N. / K


def test_redirecting_nodes_and_context_index():
    topo = worked_topology()

    assert topo.redirecting_nodes == ("2", "5")
    assert topo.K == 3
    assert topo.context_index == {"2": 1, "5": 2}
    assert topo.is_cyclic


def test_context_index_never_contains_the_start_node():
    """`start` is never an R.N., so slot 0 can never alias a redirector slot."""
    topo = worked_topology()

    assert topo.start_node_id not in topo.context_index
    assert BASELINE_CONTEXT_INDEX not in topo.context_index.values()


def test_a_dag_collapses_to_k_equals_one():
    """No redirect edges means the old DAG behaviour, exactly."""
    topo = GraphTopology.build(a_doc())

    assert topo.redirecting_nodes == ()
    assert topo.K == 1
    assert topo.is_cyclic is False
    assert topo.relevant_subgraph == {}


def test_precomputed_redirector_sets():
    topo = worked_topology()

    # Both redirectors are upstream of 3: 2 -> 3 directly, and 5 -> 2 -> 3.
    assert topo.ancestor_rns["3"] == frozenset({"2", "5"})
    # Nothing upstream of 1 redirects; 5 is its sibling, not its ancestor.
    assert topo.ancestor_rns["1"] == frozenset()
    assert topo.descendant_rns["0"] == frozenset({"2", "5"})
    assert topo.descendant_rns["4"] == frozenset()


# --------------------------------------------------------------------------- relevant subgraph


def test_relevant_subgraph_degenerate_case():
    """R.S.(0,2) happens to cover all of subgraph(0), so it cannot discriminate on its own."""
    topo = worked_topology()

    assert topo.relevant_subgraph[("0", "2")] == frozenset({"0", "1", "5", "2", "3", "4"})


def test_relevant_subgraph_discriminating_case():
    """R.S.(0,5) excludes node 1 - a sibling of 5, incomparable to it.

    This is the case that fails if `R.S.` is defined as ancestors-only or as all of subgraph(i).
    """
    topo = worked_topology()

    assert topo.relevant_subgraph[("0", "5")] == frozenset({"0", "5", "2", "3", "4"})
    assert "1" not in topo.relevant_subgraph[("0", "5")]


def test_relevant_subgraph_includes_descendants_of_the_redirector():
    """Nodes below j re-run because j recomputes and feeds them."""
    topo = worked_topology()

    for pair in (("0", "2"), ("0", "5")):
        assert {"3", "4"} <= topo.relevant_subgraph[pair]


def test_relevant_subgraph_excludes_incomparable_branches():
    """A branch off the redirect target that neither feeds j nor is fed by j takes no part."""
    graph = {"s": ["i"], "i": ["j", "side"], "j": ["leaf"], "side": ["side_leaf"]}
    topo = GraphTopology.build(
        a_doc(graph=graph, redirect_edges=[{"source": "j", "target": "i"}])
    )

    rs = topo.relevant_subgraph[("i", "j")]
    assert rs == frozenset({"i", "j", "leaf"})
    assert "side" not in rs and "side_leaf" not in rs


# --------------------------------------------------------------------------- validation


def test_a_valid_cyclic_graph_has_no_issues():
    assert worked_topology().validate() == []


def test_a_plain_dag_has_no_issues():
    assert GraphTopology.build(a_doc()).validate() == []


def test_data_edge_cycle_is_rejected():
    topo = GraphTopology.build(a_doc(graph={"s": ["a"], "a": ["b"], "b": ["a"]}))

    assert ValidationCode.data_edge_cycle in codes(topo.validate())


def test_build_does_not_raise_on_a_cyclic_data_graph():
    """Validation ships warn-only first, so a bad graph still has to be runnable."""
    topo = GraphTopology.build(a_doc(graph={"s": ["a"], "a": ["b"], "b": ["a"]}))

    assert topo.K == 1
    assert topo.descendants["a"] == frozenset({"a", "b"})


def test_multiple_roots_are_rejected():
    topo = GraphTopology.build(a_doc(graph={"r1": ["x"], "r2": ["x"], "x": ["y"]}))

    issues = topo.validate()
    assert ValidationCode.multiple_start_nodes in codes(issues)
    assert topo.start_node_id is None


def test_no_root_is_rejected():
    topo = GraphTopology.build(a_doc(graph={"a": ["b"], "b": ["a"]}))

    assert ValidationCode.no_start_node in codes(topo.validate())


def test_isolated_nodes_do_not_masquerade_as_extra_roots():
    """A node dropped on the canvas with no edges is reported as isolated, not as a second root."""
    topo = GraphTopology.build(
        a_doc(nodes={"-1": "a", "0": "b", "1": "c", "2": "d", "3": "e", "4": "f", "5": "g",
                     "orphan": "h"})
    )

    issues = topo.validate()
    assert ValidationCode.isolated_node in codes(issues)
    assert ValidationCode.multiple_start_nodes not in codes(issues)
    assert topo.start_node_id == "-1"


def test_declared_start_node_id_must_match_the_real_root():
    topo = GraphTopology.build(a_doc(start_node_id="2"))

    assert ValidationCode.start_node_id_mismatch in codes(topo.validate())


def test_a_matching_declared_start_node_id_is_fine():
    topo = GraphTopology.build(a_doc(start_node_id="-1"))

    assert topo.validate() == []


def test_redirect_to_a_non_ancestor_is_rejected():
    """`R.N. subset D.N.`: a redirect source must be reachable from its target."""
    topo = GraphTopology.build(a_doc(redirect_edges=[{"source": "1", "target": "5"}]))

    assert ValidationCode.redirect_target_not_an_ancestor in codes(topo.validate())


def test_redirect_self_loop_is_rejected():
    topo = GraphTopology.build(a_doc(redirect_edges=[{"source": "2", "target": "2"}]))

    assert ValidationCode.redirect_self_loop in codes(topo.validate())


def test_the_start_node_cannot_redirect():
    topo = GraphTopology.build(a_doc(redirect_edges=[{"source": "-1", "target": "0"}]))

    issues = topo.validate()
    assert ValidationCode.redirect_from_start_node in codes(issues)
    # The specific message, not the generic descendant failure it is implied by.
    assert ValidationCode.redirect_target_not_an_ancestor not in codes(issues)


def test_redirect_into_the_root_is_legal():
    """A redirect *to* the start node is fine - only redirecting *from* it is impossible."""
    topo = GraphTopology.build(a_doc(redirect_edges=[{"source": "2", "target": "-1"}]))

    assert topo.validate() == []
    assert topo.redirecting_nodes == ("2",)


def test_unknown_edge_endpoint_is_rejected():
    topo = GraphTopology.build(a_doc(redirect_edges=[{"source": "nope", "target": "0"}]))

    assert ValidationCode.unknown_edge_endpoint in codes(topo.validate())


def test_duplicate_redirect_edges_are_rejected():
    topo = GraphTopology.build(
        a_doc(redirect_edges=[{"source": "2", "target": "0"}, {"source": "2", "target": "0"}])
    )

    assert ValidationCode.duplicate_edge in codes(topo.validate())


def test_duplicate_conditional_edges_are_rejected():
    """Regression: `conditional` is a frozenset, so duplicates collapsed and this never fired.

    The declared tuple is kept alongside it precisely so validation can still see them.
    """
    topo = GraphTopology.build(
        a_doc(conditional_edges=[{"source": "0", "target": "5"}, {"source": "0", "target": "5"}])
    )

    assert topo.conditional_edges == (
        EdgeSpec(source="0", target="5"),
        EdgeSpec(source="0", target="5"),
    )
    assert topo.conditional == frozenset({("0", "5")})  # de-duplicated lookup set
    assert ValidationCode.duplicate_edge in codes(topo.validate())


def test_a_bad_conditional_edge_is_reported_once_even_if_duplicated():
    topo = GraphTopology.build(
        a_doc(conditional_edges=[{"source": "0", "target": "3"}, {"source": "0", "target": "3"}])
    )

    not_in_graph = [c for c in codes(topo.validate()) if c == ValidationCode.conditional_edge_not_in_graph]
    assert len(not_in_graph) == 1


def test_conditional_edge_must_exist_in_graph():
    topo = GraphTopology.build(a_doc(conditional_edges=[{"source": "0", "target": "3"}]))

    assert ValidationCode.conditional_edge_not_in_graph in codes(topo.validate())


def test_conditional_edge_on_a_real_data_edge_is_fine():
    topo = GraphTopology.build(a_doc(conditional_edges=[{"source": "0", "target": "5"}]))

    assert topo.validate() == []
    assert ("0", "5") in topo.conditional


# --------------------------------------------------------------------------- edge predicates


def test_can_redirect_from_a_descendant_back_to_its_ancestor():
    topo = GraphTopology.build(a_doc())

    assert topo.check_redirect_edge("2", "0")
    assert topo.check_redirect_edge("4", "-1")
    assert topo.check_redirect_edge("2", "0").reason is None


def test_cannot_redirect_to_a_node_that_is_not_an_ancestor():
    topo = GraphTopology.build(a_doc())

    check = topo.check_redirect_edge("1", "5")  # siblings
    assert not check
    assert check.reason.code == ValidationCode.redirect_target_not_an_ancestor


def test_cannot_redirect_in_the_forward_direction():
    topo = GraphTopology.build(a_doc())

    assert not topo.check_redirect_edge("0", "2")


def test_cannot_redirect_from_the_start_node():
    topo = GraphTopology.build(a_doc())

    check = topo.check_redirect_edge("-1", "0")
    assert check.reason.code == ValidationCode.redirect_from_start_node


def test_cannot_redirect_a_node_to_itself():
    topo = GraphTopology.build(a_doc())

    assert topo.check_redirect_edge("2", "2").reason.code == ValidationCode.redirect_self_loop


def test_cannot_redirect_using_an_unknown_node():
    topo = GraphTopology.build(a_doc())

    check = topo.check_redirect_edge("2", "ghost")
    assert check.reason.code == ValidationCode.unknown_edge_endpoint
    assert check.reason.nodes == ["ghost"]


def test_cannot_add_a_redirect_edge_that_already_exists():
    topo = worked_topology()

    assert not topo.check_redirect_edge("2", "0")
    assert topo.check_redirect_edge("2", "0").reason.code == ValidationCode.duplicate_edge
    # ...but the same edge is fine when checked as an already-declared one.
    assert topo.check_redirect_edge("2", "0", as_new=False)


def test_can_mark_an_existing_data_edge_conditional():
    topo = GraphTopology.build(a_doc())

    assert topo.check_conditional_edge("0", "5")
    assert topo.check_conditional_edge("2", "3")


def test_cannot_mark_a_non_edge_conditional():
    """A conditional edge annotates an edge already in `graph`; it does not create one."""
    topo = GraphTopology.build(a_doc())

    check = topo.check_conditional_edge("0", "3")  # 0 -> 3 is not a data edge
    assert not check
    assert check.reason.code == ValidationCode.conditional_edge_not_in_graph


def test_cannot_mark_a_reversed_edge_conditional():
    topo = GraphTopology.build(a_doc())

    assert not topo.check_conditional_edge("5", "0")  # the edge runs 0 -> 5


def test_a_conditional_edge_cannot_point_at_an_ancestor():
    """A dotted edge running backwards is a redirect by definition, whatever it is labelled."""
    topo = GraphTopology.build(a_doc())

    check = topo.check_conditional_edge("2", "0")
    assert not check
    assert check.reason.code == ValidationCode.conditional_edge_is_a_redirect
    # ...and the same pair is a perfectly good redirect edge.
    assert topo.check_redirect_edge("2", "0")


def test_the_ancestor_rule_beats_the_not_in_graph_rule():
    """Both apply to a backwards edge; the redirect message is the one that helps."""
    topo = GraphTopology.build(a_doc())

    assert "0" not in topo.data_adj["2"]  # so conditional_edge_not_in_graph would also fire
    assert (
        topo.check_conditional_edge("2", "0").reason.code
        == ValidationCode.conditional_edge_is_a_redirect
    )


def test_a_forward_conditional_edge_to_a_side_branch_is_fine():
    topo = GraphTopology.build(a_doc(graph={"a": ["b"], "b": ["c", "z"]}))

    assert topo.check_conditional_edge("b", "z")
    assert topo.check_conditional_edge("b", "c")


def test_cannot_mark_an_edge_conditional_twice():
    topo = GraphTopology.build(a_doc(conditional_edges=[{"source": "0", "target": "5"}]))

    assert topo.check_conditional_edge("0", "5").reason.code == ValidationCode.duplicate_edge
    assert topo.check_conditional_edge("0", "5", as_new=False)


def test_cannot_mark_conditional_using_an_unknown_node():
    topo = GraphTopology.build(a_doc())

    assert (
        topo.check_conditional_edge("0", "ghost").reason.code
        == ValidationCode.unknown_edge_endpoint
    )


def test_predicates_and_validate_agree():
    """The validators call the predicates, so a rule cannot be enforced in one and not the other."""
    bad_redirect = GraphTopology.build(a_doc(redirect_edges=[{"source": "1", "target": "5"}]))
    assert (
        bad_redirect.check_redirect_edge("1", "5", as_new=False).reason.code
        in codes(bad_redirect.validate())
    )

    bad_conditional = GraphTopology.build(a_doc(conditional_edges=[{"source": "0", "target": "3"}]))
    assert (
        bad_conditional.check_conditional_edge("0", "3", as_new=False).reason.code
        in codes(bad_conditional.validate())
    )


def test_an_empty_graph_does_not_explode():
    topo = GraphTopology.build(
        ActionGraphDoc(_id="g", user_id="u", name="empty")
    )

    assert topo.nodes == frozenset()
    assert topo.K == 1
    assert topo.start_node_id is None
    assert ValidationCode.no_start_node in codes(topo.validate())


def test_edge_specs_are_preserved_verbatim():
    topo = GraphTopology.build(
        a_doc(redirect_edges=[{"source": "2", "target": "0", "id": "edge-7"}])
    )

    assert topo.redirect_edges == (EdgeSpec(source="2", target="0", id="edge-7"),)
