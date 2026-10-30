"""A declined parent is left out of the input it would otherwise contribute to (OQ-9).

Part 3.5 built the verdict store and deliberately left it unread. This is the consumer. Part 5
writes the verdicts; **nothing writes one today**, so every assertion about "no verdict" here is
also the proof that landing this changes no behaviour for any graph currently running.

Asserted on the composed prompt, because that is the only place the subtraction is observable -
it is not a readiness question and nothing about scheduling changes.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from src.autobots.action_graph.action_graph.action_graph import ActionGraph
from src.autobots.action_graph.action_graph.action_graph_doc_model import EdgeSpec
from src.autobots.action_graph.action_graph.action_graph_input_creator import (
    ActionGraphInputCreator,
)
from src.autobots.action_graph.action_graph.edge_decision import record_decision
from src.autobots.action_graph.action_graph.node_result_writer import NodeRunContext
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from tests.graphs.persistence_probe import (
    a_graph_doc,
    a_probed_service,
    a_run_doc,
    an_action_result,
)

# Node "m" has two parents, so a subtraction is visible in what reaches it.
FAN_IN = {"s": ["p", "q"], "p": ["m"], "q": ["m"], "m": ["z"]}


def a_run(edge_decisions=None):
    doc = a_graph_doc(FAN_IN)
    doc.redirect_edges = [EdgeSpec(source="z", target="p")]
    run = a_run_doc(doc)
    run.result.edge_decisions = edge_decisions
    return run


# --------------------------------------------------------------------------- the helper


def test_no_verdicts_at_all_returns_the_parent_list_unchanged():
    """Every graph running today. `edge_decisions is None`, so the list comes back untouched -
    and by identity, so nothing downstream can observe a difference."""
    run = a_run()
    parents = ["p", "q"]

    result = ActionGraph.contributing_upstream(
        run, "m", parents, run_ctx=NodeRunContext(node="m", context_index="1", turn_no=0)
    )

    assert result is parents


def test_a_parent_with_no_verdict_still_contributes():
    """Absence is "no verdict", not "no". Reading it the other way would drop every parent on
    every graph, since nothing writes a verdict until Part 5."""
    decisions = record_decision(None, "m", "p", context_index="1", turn_no=0, fired=True)
    run = a_run(decisions)

    result = ActionGraph.contributing_upstream(
        run, "m", ["p", "q"], run_ctx=NodeRunContext(node="m", context_index="1", turn_no=0)
    )

    assert result == ["p", "q"], "q was never asked, so it contributes"


def test_a_declined_parent_is_dropped():
    decisions = record_decision(None, "m", "q", context_index="1", turn_no=0, fired=False)
    run = a_run(decisions)

    result = ActionGraph.contributing_upstream(
        run, "m", ["p", "q"], run_ctx=NodeRunContext(node="m", context_index="1", turn_no=0)
    )

    assert result == ["p"]


def test_a_verdict_is_scoped_to_its_own_turn():
    """Turn 0's decline must not silence the parent on turn 1 - the whole reason the key carries
    the turn."""
    decisions = record_decision(None, "m", "q", context_index="1", turn_no=0, fired=False)
    run = a_run(decisions)

    turn_1 = ActionGraph.contributing_upstream(
        run, "m", ["p", "q"], run_ctx=NodeRunContext(node="m", context_index="1", turn_no=1)
    )

    assert turn_1 == ["p", "q"]


def test_a_verdict_is_scoped_to_its_own_column():
    """`turn_no` alone is ambiguous - two columns both hold a turn 0."""
    decisions = record_decision(None, "m", "q", context_index="1", turn_no=0, fired=False)
    run = a_run(decisions)

    other_column = ActionGraph.contributing_upstream(
        run, "m", ["p", "q"], run_ctx=NodeRunContext(node="m", context_index="2", turn_no=0)
    )

    assert other_column == ["p", "q"]


# --------------------------------------------------------------------------- through the engine


async def prompts_for(node_action_id: str, edge_decisions=None) -> List[str]:
    run_doc = a_run(edge_decisions)
    service, _ = a_probed_service(run_doc)
    composed: List[Dict[str, Any]] = []
    real_to_input = ActionGraphInputCreator.to_input

    async def recording(*args, **kwargs):
        produced = await real_to_input(*args, **kwargs)
        composed.append(
            {
                "node": kwargs["current_node_action"].id,
                # The list `to_input` was actually handed, after the subtraction.
                "upstream": list(kwargs["upstream_nodes"]),
                "text": getattr(produced, "text", None),
            }
        )
        return produced

    async def fake_run_action(ctx, app_state, ua, action_id, inp, notify=None):
        return an_action_result(action_id)

    async def fake_get_action(ua, action_id, ctx=None):
        return an_action_result(action_id)

    settings = SettingsProvider.sget()
    previous = settings.ACTION_GRAPH_REDIRECT_BUDGET_DEFAULT
    object.__setattr__(settings, "ACTION_GRAPH_REDIRECT_BUDGET_DEFAULT", 2)
    try:
        with patch.object(ActionGraph, "run_action", AsyncMock(side_effect=fake_run_action)), \
             patch.object(ActionGraph, "get_action", AsyncMock(side_effect=fake_get_action)), \
             patch.object(ActionGraphInputCreator, "to_input", AsyncMock(side_effect=recording)):
            await ActionGraph.run_action_graph_v2(
                ctx=Context(), app_state=AppState(), user_actions=AsyncMock(),
                action_graph_input_dict={"text": "in"}, action_graph_result_doc=run_doc,
                action_graph_node_id=None, is_continue_run_graph=False,
                user_action_graph_result=service, notify=None,
            )
    finally:
        object.__setattr__(settings, "ACTION_GRAPH_REDIRECT_BUDGET_DEFAULT", previous)
    return [c for c in composed if c["node"] == node_action_id]


@pytest.mark.asyncio
async def test_with_no_verdicts_every_parent_reaches_every_prompt():
    """The behaviour every graph has today, pinned before the subtraction can change it."""
    runs = await prompts_for("action-action-m")

    assert runs, "node m never ran"
    for run in runs:
        assert run["upstream"] == ["p", "q"]
        assert "action-p" in run["text"] and "action-q" in run["text"]


@pytest.mark.asyncio
async def test_a_declined_parent_does_not_reach_the_prompt_for_that_turn():
    """The subtraction, observed where it is meant to be observable.

    Exactly one of node m's executions is on the turn the verdict names, so the assertion has to
    separate that one from the rest - a filter that lumped the turns together would see `q` in a
    later prompt and read the subtraction as broken. That is a real distinction, not a test
    detail: a verdict is scoped to its own turn.
    """
    # Turn 1, not 0: the forward pass owns turn 0 on every column, so the first turn a
    # redirect opens is 1. The verdict has to name the turn that actually happens.
    declined = record_decision(None, "m", "q", context_index="1", turn_no=1, fired=False)
    runs = await prompts_for("action-action-m", declined)

    without_q = [r for r in runs if r["upstream"] == ["p"]]
    with_q = [r for r in runs if r["upstream"] == ["p", "q"]]

    assert len(without_q) == 1, f"expected exactly the declined turn to drop q: {runs}"
    assert "action-q" not in without_q[0]["text"], "the declined parent still reached the prompt"
    assert with_q, "every other turn should still receive q"
    assert all("action-q" in r["text"] for r in with_q)
