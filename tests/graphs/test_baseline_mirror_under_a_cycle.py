"""The baseline mirror, exercised by a cycle whose actions do not repeat themselves.

`test_baseline_mirror.py` covers the invariant on hand-built documents. This file covers it on a
**real run**, and the distinction is the whole point: every other cyclic test in this directory
returns the same output on every call, so `baseline_matches_action_doc` compares a value to itself
and passes however wrong the write path is.

Under that fake, `output[node]` being overwritten by each re-entry is invisible. Give the actions
a counter - which is all a real action is, from the mirror's point of view - and the drift is
immediate: `assert_baseline_mirror` reported nine broken nodes on a healthy three-turn run.

So these tests exist to say two things a deterministic fake cannot:

  - `output[node]` holds the **baseline**, not whatever ran last.
  - a node outside this turn's `R.S.` contributes its **own baseline** to a child, not a value
    from whichever dimension happened to run most recently.
"""

from __future__ import annotations

from typing import Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from src.autobots.action.action.action_doc_model import ActionDoc, ActionResult
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action_graph.action_graph import action_graph as engine
from src.autobots.action_graph.action_graph.action_graph import ActionGraph
from src.autobots.action_graph.action_graph.action_graph_doc_model import EdgeSpec
from src.autobots.action_graph.action_graph.context_vector import BASELINE_SLOT
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from tests.graphs.persistence_probe import a_graph_doc, a_probed_service, a_run_doc

START = "s"

# s -> a -> b -> z, with z redirecting back into a. One R.N. (z), so K == 2 and every turn writes
# slot "1" alone - which is exactly the shape where a baseline write from a re-entry is wrong.
LOOP_GRAPH: Dict[str, List[str]] = {"s": ["a"], "a": ["b"], "b": ["z"], "z": []}
LOOP_REDIRECT = [EdgeSpec(source="z", target="a")]


def _counting_action(node: str, run_no: int) -> ActionDoc:
    """What a real action looks like to the mirror: a different answer every time."""
    text = f"{node} exec #{run_no}"
    return ActionDoc(
        _id=f"action-{node}",
        user_id="user-1",
        name=f"action {node}",
        type=ActionType.mock_action,
        input={"text": f"in {node}"},
        output={"blocks": [{"text": text}]},
        results=[ActionResult(input={"text": f"in {node}"}, output={"blocks": [{"text": text}]})],
    )


async def _run_with_counting_actions(
    graph: Dict[str, List[str]], *, redirect_edges: List[EdgeSpec] | None = None
):
    """Run a graph whose every execution returns a distinct output, watching the mirror guard."""
    graph_doc = a_graph_doc(graph)
    graph_doc.redirect_edges = redirect_edges or []
    run_doc = a_run_doc(graph_doc)
    service, _ = a_probed_service(run_doc)

    executed: List[str] = []
    drifted: List[str] = []
    real_guard = engine.assert_baseline_mirror

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        node = action_id.removeprefix("action-")
        executed.append(node)
        assert len(executed) <= 200, f"runaway cycle: {executed}"
        return _counting_action(node, len(executed))

    def watched_guard(ctx, doc, node, *, start_node_id=None, strict=False):
        held = real_guard(ctx, doc, node, start_node_id=start_node_id, strict=strict)
        if not held:
            drifted.append(node)
        return held

    with patch.object(ActionGraph, "run_action", AsyncMock(side_effect=fake_run_action)), patch.object(
        engine, "assert_baseline_mirror", watched_guard
    ):
        await ActionGraph.run_action_graph_v2(
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
    return run_doc, executed, drifted


def _text_of(payload) -> str | None:
    blocks = (payload or {}).get("blocks") or [{}]
    return blocks[0].get("text")


def _baseline_entry_text(run_doc, node: str) -> str | None:
    vector = (run_doc.result.node_vectors or {})[node]
    entries = vector.slot(BASELINE_SLOT).entries
    return _text_of(entries[-1].output) if entries else None


# --------------------------------------------------------------------------- the invariant


@pytest.mark.asyncio
async def test_the_mirror_holds_through_a_cycle_that_never_repeats_itself():
    """The test the deterministic fakes could not be: nine drift reports before the fix."""
    run_doc, executed, drifted = await _run_with_counting_actions(
        LOOP_GRAPH, redirect_edges=LOOP_REDIRECT
    )

    assert executed.count("z") > 1, "the cycle never re-entered - this proves nothing"
    assert drifted == [], (
        "output[node] and slot 0 disagree on a healthy cyclic run; "
        f"assert_baseline_mirror reported {drifted}"
    )


@pytest.mark.asyncio
async def test_output_holds_the_baseline_not_the_last_turn():
    """`output[node]` is the forward pass. The turns live in the slot, and are read from there."""
    run_doc, executed, _ = await _run_with_counting_actions(
        LOOP_GRAPH, redirect_edges=LOOP_REDIRECT
    )

    for node in ("a", "b", "z"):
        stored = _text_of(run_doc.result.output[node].output)
        assert stored == _baseline_entry_text(run_doc, node), (
            f"output[{node}] drifted from its baseline slot"
        )
        # The forward pass runs s, a, b, z as executions 1..4, so the baseline is one of those.
        # A later turn's answer here means a re-entry wrote the baseline.
        run_no = int(str(stored).rsplit("#", 1)[1])
        assert run_no <= 4, f"output[{node}] holds {stored!r}, which is a re-entry's answer"


@pytest.mark.asyncio
async def test_the_turns_are_still_recorded_in_the_redirectors_slot():
    """The negative half: keeping `output` at the baseline must not cost the transcript."""
    run_doc, executed, _ = await _run_with_counting_actions(
        LOOP_GRAPH, redirect_edges=LOOP_REDIRECT
    )

    slot = (run_doc.result.node_vectors or {})["z"].slot("1")
    texts = [_text_of(entry.output) for entry in slot.entries]
    assert len(texts) == len(set(texts)), f"a turn's answer was overwritten: {texts}"
    assert len([e for e in slot.entries if e.node_id == "z"]) > 1, (
        "the re-entries did not reach z's own dimension"
    )


# --------------------------------------------------------------------------- K == 1 is untouched


@pytest.mark.asyncio
async def test_a_dag_still_writes_every_result_to_output():
    """`for_dag` targets no slots at all, so the baseline rule must not silence it."""
    run_doc, executed, drifted = await _run_with_counting_actions(LOOP_GRAPH)

    assert executed == ["s", "a", "b", "z"]
    # A DAG keeps a vector now - one slot, K == 1 - so the guard has something to compare and
    # must still find them in step.
    assert sorted((run_doc.result.node_vectors or {})["a"].slots) == ["0"], "K == 1 is one slot"
    assert drifted == []
    for run_no, node in enumerate(("s", "a", "b", "z"), start=1):
        assert _text_of(run_doc.result.output[node].output) == f"{node} exec #{run_no}"


# ------------------------------------------------- chat with a node / single-node rerun


async def _chat_with(run_doc, service, node: str, text: str) -> None:
    """Drive `run_node` - the single-node rerun the chat and rerun buttons both use."""

    async def fake_run_action_doc(
        ctx, app_state, user_actions, action_doc, action_graph_input_dict, user, notify=None
    ):
        action_doc.results.append(
            ActionResult(input=action_graph_input_dict, output={"blocks": [{"text": text}]})
        )
        action_doc.output = {"blocks": [{"text": text}]}
        return action_doc

    with patch.object(
        ActionGraph, "run_action_doc", AsyncMock(side_effect=fake_run_action_doc)
    ):
        await ActionGraph.run_node(
            ctx=Context(),
            app_state=AppState(),
            user_actions=AsyncMock(),
            action_graph_input_dict={"text": "please redo"},
            action_graph_result_doc=run_doc,
            action_graph_node_id=node,
            user_action_graph_result=service,
            notify=None,
        )


def _slot_texts(run_doc, node: str):
    vector = (run_doc.result.node_vectors or {})[node]
    return [(e.node_id, e.turn_no, _text_of(e.output)) for e in vector.baseline.entries]


@pytest.mark.asyncio
async def test_chatting_with_a_node_records_a_turn_and_keeps_the_mirror():
    """The bug this covers: `run_node` used `for_dag`, so it wrote `output[node]` and no entry.

    `results` grew, slot 0 did not, and the two structures disagreed about the node's latest
    output - on a path `assert_baseline_mirror` is never called from, so nothing noticed.
    """
    run_doc, _, _ = await _run_with_counting_actions(LOOP_GRAPH, redirect_edges=LOOP_REDIRECT)
    service = a_probed_service(run_doc)[0]

    before = len(run_doc.result.output["b"].results)
    await _chat_with(run_doc, service, "b", "b was told to try again")

    assert len(run_doc.result.output["b"].results) == before + 1, "the chat did not run"
    assert _text_of(run_doc.result.output["b"].output) == "b was told to try again"
    assert _baseline_entry_text(run_doc, "b") == "b was told to try again", (
        "output[b] moved on and the baseline slot did not - the mirror is broken"
    )


@pytest.mark.asyncio
async def test_the_chat_is_tagged_with_the_node_and_takes_the_next_turn():
    run_doc, _, _ = await _run_with_counting_actions(LOOP_GRAPH, redirect_edges=LOOP_REDIRECT)
    service = a_probed_service(run_doc)[0]

    await _chat_with(run_doc, service, "b", "first reply")
    await _chat_with(run_doc, service, "b", "second reply")

    texts = _slot_texts(run_doc, "b")
    tags = [tag for tag, _, _ in texts]
    turns = [turn for _, turn, _ in texts]
    assert tags == [START, "b", "b"], f"expected the forward pass then two chats, got {texts}"
    assert turns == sorted(turns) and len(set(turns)) == 3, (
        f"chat turns must be distinct and sort last, got {turns}"
    )
    assert turns[1] > 0, (
        "a chat numbered from the baseline alone sorts mid-transcript in the redirector's slot"
    )
    assert texts[-1][2] == "second reply"


@pytest.mark.asyncio
async def test_the_pre_run_placeholder_does_not_leave_a_second_entry():
    """`run_node_action_with_existing_doc` records twice - empty output, then the real one.

    Both carry the same context, so `upsert_entry` replaces on identity. One execution is one
    entry, exactly as it is for a loop node reporting progressively.
    """
    run_doc, _, _ = await _run_with_counting_actions(LOOP_GRAPH, redirect_edges=LOOP_REDIRECT)
    service = a_probed_service(run_doc)[0]

    await _chat_with(run_doc, service, "b", "one reply")

    turns = [turn for _, turn, _ in _slot_texts(run_doc, "b")]
    assert len(turns) == len(set(turns)), f"the placeholder write left a duplicate: {turns}"
    assert len(turns) == 2, f"one execution must be one entry, got {turns}"


@pytest.mark.asyncio
async def test_a_dag_chat_still_writes_no_vector():
    """K == 1 keeps `for_dag`, so the run document stays byte-identical."""
    run_doc, _, _ = await _run_with_counting_actions(LOOP_GRAPH)
    service = a_probed_service(run_doc)[0]

    await _chat_with(run_doc, service, "b", "dag reply")

    assert _text_of(run_doc.result.output["b"].output) == "dag reply"
    # One slot, and the chat is in it - the same shape a cyclic chat produces, minus the
    # redirector dimensions a DAG does not have.
    vector = (run_doc.result.node_vectors or {})["b"]
    assert sorted(vector.slots) == ["0"]
    assert _text_of(vector.baseline.entries[-1].output) == "dag reply"


@pytest.mark.asyncio
async def test_a_chat_reaches_every_dimension_not_just_the_baseline():
    """A redirect target reads its OWN slot at the redirect's context index, never slot 0.

    A chat recorded on the baseline alone is invisible the moment anything redirects back into
    this node - it would be told to try again while reading a transcript that does not contain
    what the user just told it.
    """
    run_doc, _, _ = await _run_with_counting_actions(LOOP_GRAPH, redirect_edges=LOOP_REDIRECT)
    service = a_probed_service(run_doc)[0]

    await _chat_with(run_doc, service, "a", "the user said this")

    vector = (run_doc.result.node_vectors or {})["a"]
    assert len(vector.slots) > 1, "this shape must have a redirector slot or it proves nothing"
    for slot_key in vector.slots:
        texts = [_text_of(e.output) for e in vector.slot(slot_key).entries]
        assert "the user said this" in texts, f"the chat never reached slot {slot_key}"


@pytest.mark.asyncio
async def test_a_chat_sorts_last_in_the_redirectors_slot_too():
    """The turn number comes from the highest turn ANYWHERE, not from the baseline's own max.

    Numbered from the baseline alone, a chat on a graph whose slot 1 already holds turns 0..2
    takes turn 1 and sorts *between* two earlier redirect turns - reading as though the user had
    spoken in the middle of the loop.
    """
    run_doc, _, _ = await _run_with_counting_actions(LOOP_GRAPH, redirect_edges=LOOP_REDIRECT)
    service = a_probed_service(run_doc)[0]

    await _chat_with(run_doc, service, "a", "spoken last")

    vector = (run_doc.result.node_vectors or {})["a"]
    for slot_key in vector.slots:
        ordered = vector.slot(slot_key).sorted_entries(START)
        assert _text_of(ordered[-1].output) == "spoken last", (
            f"the chat does not sort last in slot {slot_key}: "
            f"{[(e.node_id, e.turn_no) for e in ordered]}"
        )


# --------------------------------------------- the floor: a redirect steps over a user turn


@pytest.mark.asyncio
async def test_a_redirect_turn_never_reuses_a_number_a_user_operation_took():
    """The reason `next_turn`'s seed is a floor rather than a default.

    A chat writes into every slot and takes a run-wide number. The redirecting node's own counter
    knows nothing about it, so without the floor the next redirect reuses that number and the two
    entries sort as equal - a loop turn and a user instruction, indistinguishable in order.

    Written against the wrong version: with `turn_no = updated.get(key, seed)` the redirect's next
    turn equals the chat's, and this fails.
    """
    graph_doc = a_graph_doc(LOOP_GRAPH)
    graph_doc.redirect_edges = LOOP_REDIRECT
    run_doc = a_run_doc(graph_doc)
    service, _ = a_probed_service(run_doc)
    counter = {"n": 0}

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        counter["n"] += 1
        return _counting_action(action_id.removeprefix("action-"), counter["n"])

    async def drive():
        with patch.object(
            ActionGraph, "run_action", AsyncMock(side_effect=fake_run_action)
        ):
            await ActionGraph.run_action_graph_v2(
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

    await drive()
    before = sorted({e.turn_no for e in run_doc.result.node_vectors["z"].slot("1").tagged("z")})
    assert before, "the cycle opened no turn, so there is nothing to step over"

    await _chat_with(run_doc, service, "b", "the user speaks")
    chat_turn = max(e.turn_no for e in run_doc.result.node_vectors["b"].baseline.entries)
    assert chat_turn > before[-1], "the chat should already sort after the loop turns"

    # Raise the exhausted budget so the cycle can open one more turn.
    for key, budget in list((run_doc.result.redirect_budgets or {}).items()):
        run_doc.result.redirect_budgets[key] = budget.model_copy(
            update={"limit": budget.limit + 1}
        )
    await drive()

    after = sorted({e.turn_no for e in run_doc.result.node_vectors["z"].slot("1").tagged("z")})
    opened_since = [turn for turn in after if turn not in before]
    assert opened_since, "raising the limit opened no further turn"
    assert min(opened_since) > chat_turn, (
        f"a redirect turn reused or undercut the chat's number {chat_turn}: {after}"
    )
