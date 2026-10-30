"""The precise upstream check (§2.5), shipped as a canary.

Two things need proving about a check that cannot currently fail:

  1. It is **wired** - a guard with no call site is not a guard, and this one is only expressible
     now that the frame names its edge.
  2. It **would** fire. A canary that has never been shown to sing is indistinguishable from one
     that is broken, so the second half of every test here breaks the document on purpose.

It logs and continues by default, raising only under `strict` - like `agree()` and the two mirror
guards. It is not a gate: `rank` is topological and execution inside a turn is sequential, so today
it can never fire. It becomes load-bearing the moment anything inside a turn runs concurrently.
"""

from __future__ import annotations

from typing import List
from unittest.mock import patch

import pytest

from src.autobots.action_graph.action_graph.action_graph_doc_model import EdgeSpec
from src.autobots.action_graph.action_graph.node_result_writer import (
    NodeRunContext,
    assert_upstream_turn_recorded,
    record_node_result,
)
from src.autobots.data_model.context import Context
from tests.graphs.persistence_probe import (
    a_graph_doc,
    a_run_doc,
    an_action_result,
)
from tests.graphs.test_cyclic_execution import run_graph

CHAIN = {"s": ["a"], "a": ["b"], "b": ["z"]}
TWO_EDGES = [EdgeSpec(source="z", target="a"), EdgeSpec(source="z", target="b")]


def a_run_with_a_turn_recorded_by(*nodes: str, tag: str = "z", turn_no: int = 0):
    """A run document in which each of `nodes` has recorded turn `(tag, turn_no)` in slot 1."""
    run_doc = a_run_doc(a_graph_doc(CHAIN))
    for node in nodes:
        record_node_result(
            run_doc,
            an_action_result(node),
            NodeRunContext(
                node=node,
                start_node_id="s",
                tag=tag,
                turn_no=turn_no,
                target_slots=("1",),
            ),
        )
    return run_doc


# --------------------------------------------------------------------------- the check itself


def test_a_parent_that_recorded_the_turn_passes():
    run_doc = a_run_with_a_turn_recorded_by("a")

    assert assert_upstream_turn_recorded(
        Context(),
        run_doc,
        "b",
        parents=["a"],
        context_index="1",
        tag="z",
        turn_no=0,
        strict=True,
    )


def test_a_parent_that_has_not_recorded_the_turn_raises_under_strict():
    """The deliberately broken version: `b` runs the turn before `a` wrote it."""
    run_doc = a_run_with_a_turn_recorded_by()  # nobody has recorded anything

    with pytest.raises(AssertionError, match="before"):
        assert_upstream_turn_recorded(
            Context(),
            run_doc,
            "b",
            parents=["a"],
            context_index="1",
            tag="z",
            turn_no=0,
            strict=True,
        )


def test_it_logs_and_continues_when_not_strict():
    """A canary that takes down production runs is a worse canary (R3-4)."""
    run_doc = a_run_with_a_turn_recorded_by()

    assert (
        assert_upstream_turn_recorded(
            Context(), run_doc, "b", parents=["a"], context_index="1", tag="z", turn_no=0
        )
        is False
    ), "it reports the failure as a return value rather than an exception"


def test_the_wrong_turn_does_not_satisfy_it():
    """Identity is `(turn_no, tag)`. A parent holding turn 0 has not recorded turn 1."""
    run_doc = a_run_with_a_turn_recorded_by("a", turn_no=0)

    with pytest.raises(AssertionError):
        assert_upstream_turn_recorded(
            Context(),
            run_doc,
            "b",
            parents=["a"],
            context_index="1",
            tag="z",
            turn_no=1,
            strict=True,
        )


def test_the_wrong_column_does_not_satisfy_it():
    """A turn recorded in another R.N.'s column says nothing about this one."""
    run_doc = a_run_with_a_turn_recorded_by("a")

    with pytest.raises(AssertionError):
        assert_upstream_turn_recorded(
            Context(),
            run_doc,
            "b",
            parents=["a"],
            context_index="2",
            tag="z",
            turn_no=0,
            strict=True,
        )


def test_a_node_with_no_parents_taking_part_passes_vacuously():
    """The caller intersects with `R.S.` first, so an empty list is the normal case, not a bug.

    A parent incomparable to the redirecting node is not in `R.S.` (D1): it does not re-run this
    turn, delivers nothing, and the node reads its stored value instead (case (ii)). Demanding an
    entry from it would make the check fire on every healthy graph with a sibling branch.
    """
    run_doc = a_run_with_a_turn_recorded_by()

    assert assert_upstream_turn_recorded(
        Context(), run_doc, "b", parents=[], context_index="1", tag="z", turn_no=0, strict=True
    )


def test_every_missing_parent_is_named_at_once():
    """One report per node, not one per parent - the operator wants the whole set."""
    run_doc = a_run_with_a_turn_recorded_by("a")

    with pytest.raises(AssertionError, match=r"\['b', 'y'\]"):
        assert_upstream_turn_recorded(
            Context(),
            run_doc,
            "z",
            parents=["a", "b", "y"],
            context_index="1",
            tag="z",
            turn_no=0,
            strict=True,
        )


# --------------------------------------------------------------------------- the wiring


@pytest.mark.asyncio
async def test_the_canary_runs_during_a_cyclic_run_and_stays_silent():
    """It is called for real, and a healthy graph never trips it.

    The ordering that makes it silent is the one it exists to watch: `rank` is topological and a
    turn runs sequentially. If either changes, this test is where the change announces itself.
    """
    import src.autobots.action_graph.action_graph.action_graph as engine

    calls: List[dict] = []
    real = engine.assert_upstream_turn_recorded

    def recording(ctx, result_doc, node, **kwargs):
        outcome = real(ctx, result_doc, node, **kwargs)
        calls.append({"node": node, "ok": outcome, **kwargs})
        return outcome

    with patch.object(engine, "assert_upstream_turn_recorded", recording):
        await run_graph(CHAIN, redirect_edges=TWO_EDGES)

    assert calls, "the canary was never called during a cyclic run"
    assert all(call["ok"] for call in calls), (
        f"the canary fired on a healthy graph: {[c for c in calls if not c['ok']]}"
    )
    assert any(call["parents"] for call in calls), (
        "every call had an empty parent set, so nothing was actually checked"
    )


@pytest.mark.asyncio
async def test_the_canary_is_not_called_on_the_forward_pass():
    """Turn 0 has no frame, and `upstream_ready` already asks the right question there."""
    import src.autobots.action_graph.action_graph.action_graph as engine

    calls: List[str] = []

    def recording(ctx, result_doc, node, **kwargs):
        calls.append(node)
        return True

    with patch.object(engine, "assert_upstream_turn_recorded", recording):
        await run_graph({"s": ["a"], "a": ["b"]})

    assert calls == [], f"a DAG has no turns, so nothing should have been checked: {calls}"


@pytest.mark.asyncio
async def test_the_canary_fires_when_a_parent_is_robbed_of_its_turn():
    """Break the invariant mid-run and watch it sing - the half a silent canary cannot prove.

    A parent's entry for the turn is deleted the moment before its child is checked, which is what
    a concurrent turn would look like from the child's side: the parent has not recorded yet.
    """
    import src.autobots.action_graph.action_graph.action_graph as engine

    fired: List[dict] = []
    real = engine.assert_upstream_turn_recorded

    def sabotage(ctx, result_doc, node, **kwargs):
        for parent in kwargs.get("parents") or ():
            vector = (result_doc.result.node_vectors or {}).get(parent)
            if vector is None:
                continue
            slot = vector.slot(kwargs["context_index"])
            kept = [
                e
                for e in slot.entries
                if e.identity() != (kwargs["turn_no"], kwargs["tag"])
            ]
            if len(kept) != len(slot.entries):
                slot.entries = kept
        outcome = real(ctx, result_doc, node, **kwargs)
        if not outcome:
            fired.append({"node": node, **kwargs})
        return outcome

    with patch.object(engine, "assert_upstream_turn_recorded", sabotage):
        await run_graph(CHAIN, redirect_edges=TWO_EDGES)

    assert fired, "the invariant was broken on purpose and the canary said nothing"


@pytest.mark.asyncio
async def test_a_healthy_cyclic_run_would_pass_the_check_under_strict():
    """Same run, raising instead of logging - so silence is a verdict and not a default."""
    import src.autobots.action_graph.action_graph.action_graph as engine

    real = engine.assert_upstream_turn_recorded

    def strictly(ctx, result_doc, node, **kwargs):
        return real(ctx, result_doc, node, **{**kwargs, "strict": True})

    with patch.object(engine, "assert_upstream_turn_recorded", strictly):
        final, _, _ = await run_graph(CHAIN, redirect_edges=TWO_EDGES)

    from src.autobots.event_result.event_result_model import EventResultStatus

    assert final.status != EventResultStatus.error, (
        "the strict canary failed a healthy cyclic run"
    )


@pytest.mark.asyncio
async def test_the_check_is_scoped_to_the_relevant_subgraph():
    """Precision, not just presence: an incomparable sibling is never demanded.

    `1` and `5` both feed `2`; `5` redirects into `0`. `1` is incomparable to `5`, so it takes no
    part in the turn and holds nothing for it. A check over all parents rather than over `R.S.`
    would fire on this graph every turn.
    """
    import src.autobots.action_graph.action_graph.action_graph as engine

    calls: List[dict] = []
    real = engine.assert_upstream_turn_recorded

    def recording(ctx, result_doc, node, **kwargs):
        outcome = real(ctx, result_doc, node, **kwargs)
        calls.append({"node": node, "ok": outcome, **kwargs})
        return outcome

    graph = {"0": ["1", "5"], "1": ["2"], "5": ["2"], "2": ["3", "4"]}
    with patch.object(engine, "assert_upstream_turn_recorded", recording):
        await run_graph(graph, redirect_edges=[EdgeSpec(source="5", target="0")])

    checks_on_2 = [call for call in calls if call["node"] == "2"]
    assert checks_on_2, "node 2 never ran a turn"
    assert all("1" not in call["parents"] for call in checks_on_2), (
        "an incomparable sibling was demanded to have recorded the turn"
    )
    assert all(call["ok"] for call in calls), "the canary fired on a healthy graph"
