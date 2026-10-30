"""What a node reads when it runs - Part 4's exit criterion 2.

**Asserted on the composed input, never on the stored document.** Part 3 already made the
transcript correct, so asserting on the transcript would flatter the result: it would pass against
the very version this part exists to fix.

The property most worth pinning is that reading is **two rules, by role**:

  - the redirect **target** reads its own slot plus the redirector's handback, and *not* its data
    parents, which sit upstream of the loop and have nothing new to say;
  - every **other** participant reads its data parents' slots, one hop.

A single rule for both is the mistake this file exists to catch. Building the target's prompt from
its data parents leaves the one node being told to try again reading a stale value and none of the
feedback.
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
from src.autobots.action_graph.action_graph.context_input import (
    context_view,
    contextual_input,
    contextual_input_dict,
)
from src.autobots.action_graph.action_graph.context_vector import (
    ContextEntry,
    ContextSlot,
    NodeContextVector,
)
from src.autobots.action_graph.action_graph.edge_decision import record_decision
from src.autobots.action_graph.action_graph.node_result_writer import (
    NodeRunContext,
    node_results,
)
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from tests.graphs.persistence_probe import (
    a_graph_doc,
    a_probed_service,
    a_run_doc,
    an_action_result,
)

START = "s"
# s -> a -> b -> z, and z sends back to a. So `a` is the redirect target and `b` is ordinary.
CHAIN = {"s": ["a"], "a": ["b"], "b": ["z"]}
REDIRECT = [EdgeSpec(source="z", target="a")]


def entry(node_id: str, turn_no: int, out: str) -> ContextEntry:
    return ContextEntry(
        node_id=node_id,
        turn_no=turn_no,
        input={"text": f"in {turn_no}"},
        output={"blocks": [{"text": out}]},
    )


def slot(*entries: ContextEntry) -> ContextSlot:
    return ContextSlot(entries=list(entries))


def a_run(*, with_vectors: bool = True, decisions=None):
    graph_doc = a_graph_doc(CHAIN)
    graph_doc.redirect_edges = REDIRECT
    run_doc = a_run_doc(graph_doc)
    run_doc.result.edge_decisions = decisions
    run_doc.result.output = {n: an_action_result(n) for n in ("s", "a", "b", "z")}
    if with_vectors:
        run_doc.result.node_vectors = {
            "a": NodeContextVector(
                slots={
                    "0": slot(entry(START, 0, "a baseline")),
                    "1": slot(entry(START, 0, "a baseline"), entry("z", 0, "a turn 0")),
                }
            ),
            "s": NodeContextVector(slots={"0": slot(entry(START, 0, "s baseline"))}),
            "b": NodeContextVector(slots={"0": slot(entry(START, 0, "b baseline"))}),
        }
    return run_doc


def ctx_for(node: str, *, target: bool, parents: List[str], turn_no: int = 0) -> NodeRunContext:
    return NodeRunContext(
        node=node,
        start_node_id=START,
        tag="z",
        turn_no=turn_no,
        target_slots=("1",),
        context_index="1",
        is_redirect_target=target,
        data_parents=tuple(parents),
    )


# --------------------------------------------------------------------------- identity cases


def test_a_dag_gets_its_own_list_and_map_back_by_identity():
    """K == 1. Same objects out, so a DAG cannot compose a different prompt however hard it tries."""
    run_doc = a_run(with_vectors=False)
    parents = ["a"]

    upstream, responses = contextual_input(
        run_doc, "b", parents, run_ctx=ctx_for("b", target=False, parents=["a"])
    )

    assert upstream == parents
    assert responses is node_results(run_doc)


def test_the_baseline_slot_gets_the_map_back_by_identity():
    """Slot 0 mirrors `output[n]`, so a cyclic graph's forward pass composes exactly what the
    identical DAG does."""
    run_doc = a_run()
    run_ctx = NodeRunContext(
        node="b", start_node_id=START, target_slots=("0",), context_index="0", data_parents=("a",)
    )

    _, responses = contextual_input(run_doc, "b", ["a"], run_ctx=run_ctx)

    assert responses is node_results(run_doc)


# --------------------------------------------------------------------------- ordinary participant


def test_an_ordinary_node_reads_its_data_parents_transcript():
    run_doc = a_run()

    upstream, responses = contextual_input(
        run_doc, "b", ["a"], run_ctx=ctx_for("b", target=False, parents=["a"])
    )

    assert upstream == ["a"], "an ordinary node keeps its data parents"
    assert "a turn 0" in str(responses["a"].output)


def test_a_parent_with_an_empty_slot_keeps_its_stored_result():
    """It is contributing the value it already has (case (ii)) - what a node outside this turn's
    R.S. legitimately does. A view built from an empty slot would hand the model a blank."""
    run_doc = a_run()
    assert not run_doc.result.node_vectors["s"].slot("1").entries

    _, responses = contextual_input(
        run_doc, "a", ["s"], run_ctx=ctx_for("a", target=False, parents=["s"])
    )

    assert responses["s"] is node_results(run_doc)["s"]


def test_the_stored_map_is_never_written_through():
    run_doc = a_run()
    stored = node_results(run_doc)["a"]

    contextual_input(run_doc, "b", ["a"], run_ctx=ctx_for("b", target=False, parents=["a"]))

    assert node_results(run_doc)["a"] is stored


# --------------------------------------------------------------------------- the redirect target


def test_the_target_reads_its_own_slot_and_not_its_data_parents():
    """The rule a single read-rule gets wrong.

    `a`'s data parent is `s`, which sits upstream of the loop and did not take part in this turn.
    Reading it would leave the node being told to try again looking at a stale value and none of
    its own history.
    """
    run_doc = a_run()

    upstream, responses = contextual_input(
        run_doc, "a", ["s"], run_ctx=ctx_for("a", target=True, parents=["s"])
    )

    assert upstream == ["a"], f"the target should read itself, not {upstream}"
    assert "s" not in upstream
    assert "a turn 0" in str(responses["a"].output), "the target cannot see its own history"


def test_the_targets_own_section_is_labelled_as_its_own():
    """`to_input` heads each section with the doc's name, so without this the target's own history
    reads as though an upstream produced it."""
    run_doc = a_run()

    _, responses = contextual_input(
        run_doc, "a", ["s"], run_ctx=ctx_for("a", target=True, parents=["s"])
    )

    assert "your work so far" in (responses["a"].name or "")


def test_a_target_with_no_history_yet_falls_back_to_its_parents():
    """Nothing of its own to read, so composing an empty prompt would be worse than the old
    behaviour. Cannot arise today - case (i) seeds every slot - but the read must not assume it."""
    run_doc = a_run()
    del run_doc.result.node_vectors["a"]

    upstream, responses = contextual_input(
        run_doc, "a", ["s"], run_ctx=ctx_for("a", target=True, parents=["s"])
    )

    assert upstream == ["s"]
    assert responses is node_results(run_doc)


# --------------------------------------------------------------------------- the handback


def test_with_no_message_the_target_reads_only_itself():
    """Nothing writes a message until Part 5, so today every target reads its own transcript alone
    rather than an empty section attributed to the redirector."""
    run_doc = a_run()

    upstream, _ = contextual_input(
        run_doc, "a", ["s"], run_ctx=ctx_for("a", target=True, parents=["s"])
    )

    assert upstream == ["a"]


def test_a_handback_message_reaches_the_target_attributed_to_the_redirector():
    """SS1.4's `k_input`. It is not any parent's output, which is why `to_input` could never reach
    it - and it is the redirector talking, so it is attributed to the redirector."""
    decisions = record_decision(
        None, "a", "z", context_index="1", turn_no=0, fired=True, message="go back and fix the tone"
    )
    run_doc = a_run(decisions=decisions)

    upstream, responses = contextual_input(
        run_doc, "a", ["s"], run_ctx=ctx_for("a", target=True, parents=["s"])
    )

    assert upstream == ["a", "z"], "own history first, then the instruction"
    assert "go back and fix the tone" in str(responses["z"].output)
    assert "sent this back to you" in (responses["z"].name or "")


def test_a_message_is_scoped_to_its_own_turn():
    decisions = record_decision(
        None, "a", "z", context_index="1", turn_no=0, fired=True, message="turn 0 only"
    )
    run_doc = a_run(decisions=decisions)

    upstream, _ = contextual_input(
        run_doc, "a", ["s"], run_ctx=ctx_for("a", target=True, parents=["s"], turn_no=1)
    )

    assert upstream == ["a"], "turn 1 must not pick up turn 0's message"


def test_the_message_is_not_the_reason():
    """`reason` is the decider accounting for its own choice (RISK-9) and is never shown to the
    target. Overloading it would put the model's private rationale into the next prompt."""
    decisions = record_decision(
        None, "a", "z", context_index="1", turn_no=0, fired=True, reason="tone score below 0.4"
    )
    run_doc = a_run(decisions=decisions)

    upstream, responses = contextual_input(
        run_doc, "a", ["s"], run_ctx=ctx_for("a", target=True, parents=["s"])
    )

    assert upstream == ["a"]
    assert "tone score" not in str(responses.get("z", ""))


# --------------------------------------------------------------------------- a target with no parents

# `a` is the root AND the redirect target - the "go back to the beginning and try again" shape,
# which is the most ordinary loop anyone draws. Such a node has an empty parent list, so it never
# reaches `to_input` at all: `run_action_simple` and the loop variants take the graph input
# straight to the action. Its history has to arrive through that input instead.
ROOT_LOOP = {"a": ["b"], "b": ["c"]}
ROOT_REDIRECT = [EdgeSpec(source="c", target="a")]


def a_root_run(*, history: bool = True, decisions=None):
    graph_doc = a_graph_doc(ROOT_LOOP)
    graph_doc.redirect_edges = ROOT_REDIRECT
    run_doc = a_run_doc(graph_doc)
    run_doc.result.edge_decisions = decisions
    run_doc.result.output = {n: an_action_result(n) for n in ("a", "b", "c")}
    if history:
        run_doc.result.node_vectors = {
            "a": NodeContextVector(
                slots={"1": slot(entry("a", 0, "draft one"), entry("c", 0, "draft two"))}
            )
        }
    return run_doc


def root_ctx(*, target: bool = True, turn_no: int = 0) -> NodeRunContext:
    return NodeRunContext(
        node="a", start_node_id="a", tag="c", turn_no=turn_no, target_slots=("1",),
        context_index="1", is_redirect_target=target, data_parents=(),
    )


def test_a_node_that_is_not_a_redirect_target_gets_its_input_back_by_identity():
    """Every node in a DAG, and every node on a forward pass."""
    run_doc = a_root_run()
    original = {"text": "the brief"}

    assert contextual_input_dict(
        run_doc, "a", original, run_ctx=root_ctx(target=False)
    ) is original


def test_a_target_with_no_history_yet_gets_its_input_back_by_identity():
    run_doc = a_root_run(history=False)
    original = {"text": "the brief"}

    assert contextual_input_dict(run_doc, "a", original, run_ctx=root_ctx()) is original


def test_a_root_redirect_target_receives_its_own_transcript():
    """The bug this covers: without it the root re-runs on byte-identical input every turn while
    its slot records each one - it records its history and never reads it."""
    run_doc = a_root_run()

    composed = contextual_input_dict(run_doc, "a", {"text": "the brief"}, run_ctx=root_ctx())

    assert "draft one" in composed["text"] and "draft two" in composed["text"]


def test_the_standing_instruction_still_comes_first():
    """The graph input is the brief the node is working to; its own drafts come after it."""
    run_doc = a_root_run()

    text = contextual_input_dict(run_doc, "a", {"text": "the brief"}, run_ctx=root_ctx())["text"]

    assert text.startswith("the brief")
    assert text.index("the brief") < text.index("draft one")


def test_urls_from_the_transcript_are_carried_and_not_duplicated():
    run_doc = a_root_run()
    run_doc.result.node_vectors["a"] = NodeContextVector(
        slots={
            "1": slot(
                ContextEntry(
                    node_id="c", turn_no=0,
                    output={"blocks": [{"text": "d", "urls": ["s3://a.pdf", "s3://keep.pdf"]}]},
                )
            )
        }
    )

    composed = contextual_input_dict(
        run_doc, "a", {"text": "brief", "urls": ["s3://a.pdf"]}, run_ctx=root_ctx()
    )

    assert composed["urls"] == ["s3://a.pdf", "s3://keep.pdf"]


def test_a_handback_message_reaches_a_root_target_too():
    decisions = record_decision(
        None, "a", "c", context_index="1", turn_no=0, fired=True, message="tighten the opening"
    )
    run_doc = a_root_run(decisions=decisions)

    text = contextual_input_dict(run_doc, "a", {"text": "brief"}, run_ctx=root_ctx())["text"]

    assert "tighten the opening" in text
    assert text.index("draft one") < text.index("tighten the opening"), (
        "what it is being asked to change should come after what it produced"
    )


def test_the_original_input_dict_is_not_mutated():
    run_doc = a_root_run()
    original = {"text": "the brief", "urls": []}

    contextual_input_dict(run_doc, "a", original, run_ctx=root_ctx())

    assert original == {"text": "the brief", "urls": []}


@pytest.mark.asyncio
async def test_end_to_end_a_root_target_stops_re_running_on_identical_input():
    """Through the engine, on the shape `test_crash_mid_turn.py` already uses."""
    graph_doc = a_graph_doc(ROOT_LOOP)
    graph_doc.redirect_edges = ROOT_REDIRECT
    run_doc = a_run_doc(graph_doc)
    service, _ = a_probed_service(run_doc)
    inputs: List[Dict[str, Any]] = []

    async def fake(ctx, app_state, ua, action_id, inp, notify=None):
        if action_id == "action-a":
            inputs.append(dict(inp))
        return an_action_result(action_id)

    settings = SettingsProvider.sget()
    previous = settings.ACTION_GRAPH_REDIRECT_BUDGET_DEFAULT
    object.__setattr__(settings, "ACTION_GRAPH_REDIRECT_BUDGET_DEFAULT", 2)
    try:
        with patch.object(ActionGraph, "run_action", AsyncMock(side_effect=fake)):
            await ActionGraph.run_action_graph_v2(
                ctx=Context(), app_state=AppState(), user_actions=AsyncMock(),
                action_graph_input_dict={"text": "ORIGINAL"}, action_graph_result_doc=run_doc,
                action_graph_node_id=None, is_continue_run_graph=False,
                user_action_graph_result=service, notify=None,
            )
    finally:
        object.__setattr__(settings, "ACTION_GRAPH_REDIRECT_BUDGET_DEFAULT", previous)

    assert len(inputs) > 1, "the root was never re-entered"
    assert inputs[0]["text"] == "ORIGINAL", "the forward pass reads the brief alone"
    texts = [i["text"] for i in inputs]
    assert len(set(texts)) == len(texts), f"the root re-ran on identical input: {texts}"
    for later in texts[1:]:
        assert "Turn" in later and later.startswith("ORIGINAL")


# --------------------------------------------------------------------------- the view


def test_a_view_does_not_mutate_the_stored_document():
    doc = an_action_result("action-a")
    before = doc.model_dump()

    context_view(doc, slot(entry("z", 0, "new")), start_node_id=START)

    assert doc.model_dump() == before


def test_a_view_carries_every_turn_and_labels_each():
    doc = an_action_result("action-a")
    transcript = slot(entry(START, 0, "draft one"), entry("z", 1, "draft two"))

    view = context_view(doc, transcript, start_node_id=START)

    assert len(view.results) == 2
    assert "initial pass" in str(view.results[0].output)
    assert "redirected by node z" in str(view.results[1].output)


# --------------------------------------------------------------------------- end to end


async def a_run_capturing_composed_input(redirect_edges, budget: int = 2):
    graph_doc = a_graph_doc(CHAIN)
    graph_doc.redirect_edges = redirect_edges
    run_doc = a_run_doc(graph_doc)
    service, _ = a_probed_service(run_doc)

    composed: List[Dict[str, Any]] = []
    real_to_input = ActionGraphInputCreator.to_input

    # `run_action_with_loop` passes ctx positionally, `run_action_with_merge` by keyword.
    async def recording(*args, **kwargs):
        produced = await real_to_input(*args, **kwargs)
        composed.append(
            {
                # `an_action_result` prefixes an already-prefixed id, so this reads "action-action-b"
                "node": kwargs["current_node_action"].id,
                "upstream": list(kwargs["upstream_nodes"]),
                "text": getattr(produced, "text", None) or "",
            }
        )
        return produced

    async def fake_run_action(ctx, app_state, ua, action_id, inp, notify=None):
        return an_action_result(action_id)

    async def fake_get_action(ua, action_id, ctx=None):
        return an_action_result(action_id)

    settings = SettingsProvider.sget()
    previous = settings.ACTION_GRAPH_REDIRECT_BUDGET_DEFAULT
    object.__setattr__(settings, "ACTION_GRAPH_REDIRECT_BUDGET_DEFAULT", budget)
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
    return composed


@pytest.mark.asyncio
async def test_end_to_end_the_target_stops_reading_the_node_above_the_loop():
    """`a`'s parent `s` is outside the loop. On the forward pass `a` reads it; on every re-entry it
    reads itself instead. This is the behaviour change the whole part exists for."""
    composed = await a_run_capturing_composed_input(REDIRECT)
    runs = [c for c in composed if c["node"] == "action-action-a"]

    assert len(runs) > 1, f"node a was never re-entered: {composed}"
    assert runs[0]["upstream"] == ["s"], "the forward pass reads its parent, as it always did"
    for later in runs[1:]:
        assert later["upstream"] == ["a"], "a re-entry must read its own history, not `s`"
        assert "Turn" in later["text"]


@pytest.mark.asyncio
async def test_end_to_end_an_ordinary_node_keeps_reading_its_parent():
    composed = await a_run_capturing_composed_input(REDIRECT)
    runs = [c for c in composed if c["node"] == "action-action-b"]

    assert len(runs) > 1
    for run in runs:
        assert run["upstream"] == ["a"], "an ordinary node always reads its data parents"
    assert "Turn" in runs[-1]["text"], "and sees their history once there is some"


@pytest.mark.asyncio
async def test_a_graph_with_no_redirect_edges_composes_exactly_what_it_always_did():
    """K == 1 is byte-identical - the property Parts 1 to 3.5 all held."""
    composed = await a_run_capturing_composed_input([])

    for record in composed:
        assert "Turn" not in record["text"], (
            f"a DAG composed a transcript for {record['node']}:\n{record['text']}"
        )
