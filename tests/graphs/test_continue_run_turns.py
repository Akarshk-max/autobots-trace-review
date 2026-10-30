"""A continue-run records a turn, and only for the nodes that actually re-run.

Two rules meet here, and they are easy to get wrong in opposite directions.

**The turn.** A continue-run used to re-execute a subtree under `(start, turn 0)` - the identity
the forward pass already holds - so `upsert_entry` replaced that cell and the earlier output was
gone. `ActionDoc.results` grew one entry per execution while the slot stayed at one. It now takes a
fresh run-wide turn, tagged with the node it was started from, so the slot mirrors `results`.

**Who takes part.** Readiness on the baseline column is a turn comparison, so a parent has to hold
*this* turn. But two kinds of parent never will: the continue-run **root**, which is deliberately
skipped, and a parent that lies **outside the subtree** entirely - `upstream_nodes` comes from the
full inverted map, not the subgraph. Asking them the strict question stalls the whole run, and an
earlier version had them write a carry-forward entry to answer it. That entry was write-only
(nothing reads a slot during a continue-run - `contextual_input` returns by identity at the
baseline) and it filed a value the node had not produced. They get the loose question instead.

These are also the first tests in the repo that drive `is_continue_run_graph=True`.
"""

from __future__ import annotations

from typing import Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from src.autobots.action.action.action_doc_model import ActionDoc, ActionResult
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action_graph.action_graph.action_graph import ActionGraph
from src.autobots.action_graph.action_graph.action_graph_doc_model import EdgeSpec
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from tests.graphs.persistence_probe import a_graph_doc, a_probed_service, a_run_doc

START = "s"
ROOT = "a"

CHAIN: Dict[str, List[str]] = {"s": ["a"], "a": ["b"], "b": ["z"], "z": []}
# `c` joins on `a` (inside the subtree from `a`) and on `p` (outside it) - the shape where a
# strict check against every parent deadlocks on a node that was never asked to run.
JOIN: Dict[str, List[str]] = {"s": ["a", "p"], "a": ["c"], "p": ["c"], "c": ["z"], "z": []}
REDIRECT = [EdgeSpec(source="z", target="a")]


async def a_run_then_continue(
    graph: Dict[str, List[str]],
    *,
    redirect_edges: List[EdgeSpec] | None = None,
    root: str = ROOT,
    times: int = 1,
):
    """Forward run, then `times` continue-runs from `root`. Returns the doc and what re-ran."""
    graph_doc = a_graph_doc(graph)
    graph_doc.redirect_edges = redirect_edges or []
    run_doc = a_run_doc(graph_doc)
    service, _ = a_probed_service(run_doc)
    counter = {"n": 0}
    reran: List[str] = []

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        counter["n"] += 1
        node = action_id.removeprefix("action-")
        text = f"{node}#{counter['n']}"
        return ActionDoc(
            _id=action_id,
            user_id="user-1",
            name=f"action {node}",
            type=ActionType.mock_action,
            input={"text": f"in {node}"},
            output={"blocks": [{"text": text}]},
            results=[
                ActionResult(input={"text": f"in {node}"}, output={"blocks": [{"text": text}]})
            ],
        )

    async def fake_run_action_doc(
        ctx, app_state, user_actions, action_doc, action_graph_input_dict, user, notify=None
    ):
        node = (action_doc.name or "?").removeprefix("action ")
        reran.append(node)
        action_doc.results.append(
            ActionResult(
                input=action_graph_input_dict, output={"blocks": [{"text": f"{node} cont"}]}
            )
        )
        action_doc.output = {"blocks": [{"text": f"{node} cont"}]}
        return action_doc

    async def drive(node_id, continue_run):
        with patch.object(
            ActionGraph, "run_action", AsyncMock(side_effect=fake_run_action)
        ), patch.object(
            ActionGraph, "run_action_doc", AsyncMock(side_effect=fake_run_action_doc)
        ):
            await ActionGraph._run_retry_as_background_task(
                ctx=Context(),
                app_state=AppState(),
                user_actions=AsyncMock(),
                action_graph_input_dict={"text": "in"},
                action_graph_result_doc=run_doc,
                user_action_graph_result=service,
                notify=None,
                action_graph_node_id=node_id,
                is_rerun_graph=False,
                is_continue_run_graph=continue_run,
            )

    await drive(None, False)
    reran.clear()
    for _ in range(times):
        await drive(root, True)
    return run_doc, reran


def baseline(run_doc, node: str):
    vector = (run_doc.result.node_vectors or {}).get(node)
    return [] if vector is None else [(e.node_id, e.turn_no) for e in vector.baseline.entries]


# --------------------------------------------------------------------------- the turn


@pytest.mark.asyncio
async def test_a_continue_run_appends_instead_of_overwriting_turn_zero():
    """One slot entry per execution, which is what `results` counts."""
    run_doc, reran = await a_run_then_continue(CHAIN, redirect_edges=REDIRECT, times=2)

    assert reran == ["b", "z", "b", "z"], f"the subtree did not re-run twice: {reran}"
    for node in ("b", "z"):
        entries = baseline(run_doc, node)
        assert len(entries) == len(run_doc.result.output[node].results), (
            f"{node}: slot {entries} does not mirror its {len(run_doc.result.output[node].results)} results"
        )
        assert entries[0] == (START, 0), "the forward pass entry was overwritten"
        assert len({turn for _, turn in entries}) == len(entries), f"a turn repeated: {entries}"


@pytest.mark.asyncio
async def test_every_node_in_one_continue_run_shares_its_turn():
    """The number is fixed once for the operation, not recomputed per node - which is what lets a
    child ask "is my parent at turn N?" and mean anything by it."""
    run_doc, _ = await a_run_then_continue(CHAIN, redirect_edges=REDIRECT)

    turns = {node: baseline(run_doc, node)[-1][1] for node in ("b", "z")}
    assert len(set(turns.values())) == 1, f"the participants disagree about the turn: {turns}"


# --------------------------------------------------------------------------- who takes part


@pytest.mark.asyncio
async def test_the_root_gains_no_vector_entry():
    """The root is skipped, so nothing about it changed and nothing should be filed on its behalf.

    Its vector is filled by `a` actually executing - a chat, or a plain run - never by a
    continue-run rooted at it.
    """
    run_doc, _ = await a_run_then_continue(CHAIN, redirect_edges=REDIRECT, times=2)

    assert baseline(run_doc, ROOT) == [(START, 0)], (
        "the continue-run wrote an entry for a node that never ran"
    )
    assert len(run_doc.result.output[ROOT].results) == 1


@pytest.mark.asyncio
async def test_a_node_whose_parent_is_outside_the_subtree_still_runs():
    """The deadlock this scoping exists to prevent.

    `c` joins on `a` (the skipped root) and `p` (outside the subtree). Neither will ever record
    this turn. A strict check against both leaves `c` never ready, nothing executes, and the run
    still reports success - a silent no-op.
    """
    run_doc, reran = await a_run_then_continue(JOIN, redirect_edges=REDIRECT)

    assert reran == ["c", "z"], f"the join stalled the continue-run: {reran or 'NOTHING RAN'}"


@pytest.mark.asyncio
async def test_a_node_outside_the_subtree_is_untouched():
    run_doc, _ = await a_run_then_continue(JOIN, redirect_edges=REDIRECT)

    assert baseline(run_doc, "p") == [(START, 0)], "a node outside the subtree gained an entry"
    assert len(run_doc.result.output["p"].results) == 1


# --------------------------------------------------------------------------- K == 1


@pytest.mark.asyncio
async def test_a_dag_continue_run_re_runs_and_keeps_no_vector():
    """K == 1 has no vector and therefore no turn, so it still needs the explicit done-bypass."""
    run_doc, reran = await a_run_then_continue(CHAIN, times=2)

    assert reran == ["b", "z", "b", "z"]
    assert len(run_doc.result.output["b"].results) == 3, "results should still append"
    # K == 1 is one slot, and it mirrors `results` one for one - the property that makes the
    # acyclic and cyclic paths the same code rather than two engines.
    vector = (run_doc.result.node_vectors or {})["b"]
    assert sorted(vector.slots) == ["0"]
    assert len(vector.baseline.entries) == 3


# ------------------------------------------------- the graph that cannot have a vector


@pytest.mark.asyncio
async def test_a_graph_with_no_start_node_still_runs_and_keeps_no_vector():
    """Every entry carries a provenance tag, and the start node is what tags the baseline.

    A graph with no single root has nothing to tag with, so it keeps no vector and behaves
    exactly as it did before. That is why the gate is "has a start node" rather than "is a DAG":
    one isolated node and a pure data cycle are both this shape, both legal while validation is
    warn-only, and refusing them would break runs that work today.
    """
    two_roots: Dict[str, List[str]] = {"p": ["c"], "q": ["c"], "c": []}
    run_doc, _ = await a_run_then_continue(two_roots, root="p", times=0)

    assert set(run_doc.result.output) == {"p", "q", "c"}, "the run did not complete"
    assert run_doc.result.node_vectors is None, (
        "a graph with no start node gained a vector it has no tag for"
    )
