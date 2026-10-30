"""A cycle actually executes. The first tests in the project for which that is true.

Part 3 makes a cycle **execute and record**; Part 4 makes it **read** its own transcript
(`to_input` still builds a prompt from baseline outputs). These tests are written so that
distinction cannot silently flatter the result: they assert on the recorded transcript, never on
whether the prompt looked right.
"""

from __future__ import annotations

from typing import Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from src.autobots.action_graph.action_graph.action_graph import ActionGraph
from src.autobots.action_graph.action_graph.action_graph_doc_model import EdgeSpec
from src.autobots.action_graph.action_graph.context_vector import BASELINE_SLOT
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from tests.graphs.persistence_probe import (
    a_graph_doc,
    a_probed_service,
    a_run_doc,
    an_action_result,
)

START = "s"


async def run_graph(
    graph: Dict[str, List[str]],
    *,
    redirect_edges: List[EdgeSpec] | None = None,
    max_executions: int = 200,
):
    """Run a graph end to end, recording which node ran in which order."""
    graph_doc = a_graph_doc(graph)
    graph_doc.redirect_edges = redirect_edges or []
    run_doc = a_run_doc(graph_doc)
    service, probe = a_probed_service(run_doc)

    executed: List[str] = []

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        executed.append(action_id.removeprefix("action-"))
        assert len(executed) <= max_executions, f"runaway cycle: {executed}"
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
    return final, executed, probe


# --------------------------------------------------------------------------- the DAG guarantee


@pytest.mark.asyncio
async def test_a_graph_with_no_redirect_edges_opens_no_turn_and_keeps_one_dimension():
    """K == 1: one execution per node, one slot, and none of the redirect machinery.

    **This used to assert `node_vectors is None`.** A DAG now records a transcript too, so the
    two shapes run the same code rather than a vector-writing engine beside a vector-less one -
    at K == 1 that is slot 0 alone, mirroring `ActionDoc.results`. What is still true, and is
    what this test is really for, is that nothing about *redirects* is touched: no budgets, no
    turn counters, no second dimension.
    """
    final, executed, _ = await run_graph({"s": ["a"], "a": ["b"]})

    assert executed == ["s", "a", "b"]
    assert final.result.redirect_budgets is None
    assert final.result.turn_counters is None, "a DAG opens no turns, so it counts none"
    for node, vector in (final.result.node_vectors or {}).items():
        assert sorted(vector.slots) == ["0"], f"{node} has a dimension a DAG cannot have"
        assert len(vector.baseline.entries) == 1, f"{node} ran once, so it records once"


# --------------------------------------------------------------------------- a single-node cycle


@pytest.mark.asyncio
async def test_a_single_node_cycle_re_enters_and_terminates():
    """`a` redirects back into itself. It must run more than once, and must stop."""
    final, executed, _ = await run_graph(
        {"s": ["a"], "a": ["b"]}, redirect_edges=[EdgeSpec(source="a", target="a")]
    )

    assert executed.count("a") > 1, "the cycle never re-entered - `a` ran once"
    assert len(executed) < 200, "the cycle never terminated"
    assert final.result.node_vectors is not None, "a cyclic run must record a vector"


@pytest.mark.asyncio
async def test_case_one_seeds_every_slot_then_case_two_writes_only_its_own():
    """§1.4's asymmetry, read off the recorded transcript.

    The forward pass finds every slot empty and writes the same result into all of them (case (i)),
    tagged with the start node at turn 0. A re-entry finds them populated and writes only the slot
    its R.N. owns (case (ii)), tagged with the redirecting node.
    """
    final, _, _ = await run_graph(
        {"s": ["a"], "a": ["b"]}, redirect_edges=[EdgeSpec(source="a", target="a")]
    )

    vector = final.result.node_vectors["a"]
    assert set(vector.slots) == {BASELINE_SLOT, "1"}, "K = 2, so two slots"

    baseline_tags = {e.node_id for e in vector.slot(BASELINE_SLOT).entries}
    assert baseline_tags == {START}, "only the forward pass writes the baseline here"

    own_slot = vector.slot("1")
    assert {e.node_id for e in own_slot.entries} == {START, "a"}, (
        "case (i) seeded it with the start tag; case (ii) added the redirect's"
    )


@pytest.mark.asyncio
async def test_turns_are_numbered_per_tag_and_do_not_collide():
    """Each turn records a distinct turn, so `f_comb`'s identity `(turn_no, tag)` stays unique."""
    final, _, _ = await run_graph(
        {"s": ["a"], "a": ["b"]}, redirect_edges=[EdgeSpec(source="a", target="a")]
    )

    entries = final.result.node_vectors["a"].slot("1").tagged("a")
    turns = [e.turn_no for e in entries]

    # Unique and increasing is the whole invariant. Density is NOT: turn numbers are drawn from a
    # run-wide floor, so a column skips whatever other columns and user operations consumed, and
    # the forward pass owns turn 0 so a redirect's first turn is 1. `f_comb` identifies an entry by
    # exact `(turn_no, tag)` and ordering only needs them to sort.
    assert turns == sorted(set(turns)), f"turns collided or went backwards: {turns}"
    assert turns, "the redirect recorded no turn at all"


# --------------------------------------------------------------------------- the re-run set


@pytest.mark.asyncio
async def test_an_incomparable_sibling_does_not_re_run():
    """R.S.(0,5) - the shape that shows the rule biting.

    `1` and `5` are siblings that both feed `2`; `5` redirects into `0`. `1` is incomparable to `5`
    - neither feeds it nor is fed by it - so it is not in R.S. and must not re-run. Testing only
    `R.S.(0,2)` would pass under a wrong rule, because there every node happens to be comparable.
    """
    graph = {START: ["0"], "0": ["1", "5"], "1": ["2"], "5": ["2"], "2": ["3"], "3": ["4"]}

    _, executed, _ = await run_graph(
        graph, redirect_edges=[EdgeSpec(source="5", target="0")]
    )

    assert executed.count("5") > 1, "the redirecting node never re-entered"
    assert executed.count("1") == 1, (
        f"node 1 is incomparable to 5 and must not re-run; ran {executed.count('1')}x"
    )
    assert executed.count("2") > 1, "node 2 is a descendant of 5 and must re-run"


@pytest.mark.asyncio
async def test_the_degenerate_shape_re_runs_everything_under_the_target():
    """R.S.(0,2): every node under `0` is comparable to `2`, so R.S. collapses to subgraph(0).

    Kept alongside the test above precisely because on its own it proves nothing.
    """
    graph = {START: ["0"], "0": ["1", "5"], "1": ["2"], "5": ["2"], "2": ["3"]}

    _, executed, _ = await run_graph(
        graph, redirect_edges=[EdgeSpec(source="2", target="0")]
    )

    for node in ("0", "1", "5", "2"):
        assert executed.count(node) > 1, f"{node} is in R.S.(0,2) and should re-run"


# --------------------------------------------------------------------------- durability


@pytest.mark.asyncio
async def test_the_run_records_what_the_cycle_did():
    """Everything the scheduler decided has to survive on the document, not in memory."""
    final, _, probe = await run_graph(
        {"s": ["a"], "a": ["b"]}, redirect_edges=[EdgeSpec(source="a", target="a")]
    )

    import json

    stored = json.loads(probe.stored_document("run-1"))
    assert stored["result"]["node_vectors"] is not None
    assert set(stored["result"]["node_vectors"]) == set(stored["result"]["output"]), (
        "output and node_vectors must cover the same nodes"
    )
    assert probe.snapshot()["s3_gets"] == 0, "Part 2's no-read property still holds"


# --------------------------------------------------------------------------- budget exhaustion


@pytest.mark.asyncio
async def test_an_exhausted_budget_parks_the_run_as_waiting():
    """D5: pause and ask a human, rather than silently truncating the loop."""
    from src.autobots.event_result.event_result_model import EventResultStatus

    final, _, _ = await run_graph(
        {"s": ["a"], "a": ["b"]}, redirect_edges=[EdgeSpec(source="a", target="a")]
    )

    assert final.status == EventResultStatus.waiting, (
        "a truncated cycle reported success - nobody would know the loop was cut short"
    )
    assert final.result.redirect_budgets["a|a"].is_exhausted


@pytest.mark.asyncio
async def test_the_rest_of_the_graph_still_finishes_when_a_budget_runs_out():
    """Exhaustion is not the sticky user-review gate.

    A redirect that cannot fire does not stop unrelated work from completing; the run drains and
    *then* reports `waiting`.
    """
    graph = {START: ["a", "x"], "a": ["b"], "x": ["y"]}

    final, executed, _ = await run_graph(
        graph, redirect_edges=[EdgeSpec(source="a", target="a")]
    )

    assert set(final.result.output) == {START, "a", "b", "x", "y"}, (
        f"unrelated nodes were skipped: only {sorted(final.result.output)} ran"
    )
    assert "y" in executed


@pytest.mark.asyncio
async def test_raising_the_limit_and_re_running_continues_the_cycle():
    """The resume path: same result id, more budget, more turns - not a restart."""
    graph_doc = a_graph_doc({"s": ["a"], "a": ["b"]})
    graph_doc.redirect_edges = [EdgeSpec(source="a", target="a")]
    run_doc = a_run_doc(graph_doc)
    service, _ = a_probed_service(run_doc)

    executed: List[str] = []

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        executed.append(action_id.removeprefix("action-"))
        assert len(executed) <= 300, "runaway"
        return an_action_result(action_id)

    async def run_once():
        with patch.object(ActionGraph, "run_action", AsyncMock(side_effect=fake_run_action)):
            return await ActionGraph.run_action_graph_v2(
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

    final = await run_once()
    first_pass = list(executed)
    fired_before = final.result.redirect_budgets["a|a"].fired
    turns_before = len(final.result.node_vectors["a"].slot("1").tagged("a"))

    # What a human does on the waiting run: raise the pair's limit, re-run the same result id.
    final.result.redirect_budgets["a|a"] = final.result.redirect_budgets["a|a"].model_copy(
        update={"limit": fired_before + 2}
    )
    final = await run_once()

    assert len(executed) > len(first_pass), "the resumed run did no further work"
    assert final.result.redirect_budgets["a|a"].fired > fired_before, (
        "the counter restarted instead of resuming"
    )
    turns_after = len(final.result.node_vectors["a"].slot("1").tagged("a"))
    assert turns_after > turns_before, "the resumed turns overwrote the transcript instead of adding"


@pytest.mark.asyncio
async def test_the_counter_resumes_rather_than_restarting(monkeypatch):
    """RISK-4: three retry layers stack, so an in-memory counter would never bound anything."""
    from src.autobots import SettingsProvider

    monkeypatch.setattr(
        SettingsProvider.sget(), "ACTION_GRAPH_REDIRECT_BUDGET_DEFAULT", 2, raising=False
    )

    graph_doc = a_graph_doc({"s": ["a"], "a": ["b"]})
    graph_doc.redirect_edges = [EdgeSpec(source="a", target="a")]
    run_doc = a_run_doc(graph_doc)
    service, _ = a_probed_service(run_doc)

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        return an_action_result(action_id)

    async def attempt():
        with patch.object(ActionGraph, "run_action", AsyncMock(side_effect=fake_run_action)):
            return await ActionGraph.run_action_graph_v2(
                ctx=Context(), app_state=AppState(), user_actions=AsyncMock(),
                action_graph_input_dict={"text": "in"}, action_graph_result_doc=run_doc,
                action_graph_node_id=None, is_continue_run_graph=False,
                user_action_graph_result=service, notify=None,
            )

    await attempt()
    fired = run_doc.result.redirect_budgets["a|a"].fired
    assert fired == 2, fired

    # A retry of the same run: the counter is on the document, so it does not start over.
    await attempt()

    assert run_doc.result.redirect_budgets["a|a"].fired == 2, (
        "the retry re-fired the redirect - the budget is not actually bounding anything"
    )


# --------------------------------------------------------------------------- the mirror guards


@pytest.mark.asyncio
async def test_the_mirror_guards_are_actually_called_during_a_cyclic_run():
    """Part 1 shipped these with no call site because nothing wrote a vector yet.

    A guard that is never called is not a guard, so this asserts the wiring rather than the logic
    (which `test_baseline_mirror.py` already covers).
    """
    from unittest.mock import MagicMock

    import src.autobots.action_graph.action_graph.action_graph as engine

    per_node = MagicMock(return_value=True)
    whole_run = MagicMock(return_value=True)

    with patch.object(engine, "assert_baseline_mirror", per_node), patch.object(
        engine, "assert_vector_covers_output", whole_run
    ):
        await run_graph(
            {"s": ["a"], "a": ["b"]}, redirect_edges=[EdgeSpec(source="a", target="a")]
        )

    assert per_node.called, "the per-node mirror check never ran"
    assert whole_run.called, "the whole-run coverage check never ran"

    checked = {call.args[2] for call in per_node.call_args_list}
    assert "a" in checked, f"the cycling node was never checked; saw {checked}"
    assert all(
        call.kwargs.get("start_node_id") == START for call in per_node.call_args_list
    ), "the guard cannot compare a baseline without the start node"


@pytest.mark.asyncio
async def test_the_coverage_guard_notices_drift_injected_mid_run():
    """The guard has to report on the document the engine actually produced."""
    from src.autobots.action_graph.action_graph.node_result_writer import (
        assert_vector_covers_output,
    )

    final, _, _ = await run_graph(
        {"s": ["a"], "a": ["b"]}, redirect_edges=[EdgeSpec(source="a", target="a")]
    )

    assert assert_vector_covers_output(Context(), final), "a healthy run must pass"

    final.result.output.pop("b")  # popped from one structure only

    assert not assert_vector_covers_output(Context(), final)


# --------------------------------------------------------------------------- crash durability


@pytest.mark.asyncio
async def test_a_fired_redirect_is_durable_before_the_next_node_runs():
    """No node may start while a fired redirect's increment is still only in memory.

    That gap is the crash window: the work is durable but the count is not, so a retry re-fires a
    redirect that already fired - exactly what RISK-4's durable counters exist to prevent.

    The observable has to be the gap itself, not the end state. Comparing final memory against
    final storage passes under either ordering, because the run's closing status save flushes the
    increment anyway - it just does so a node too late. So this samples at the only moment that
    distinguishes them: the instant before each node executes.
    """
    import json

    graph_doc = a_graph_doc({"s": ["a"], "a": ["b"]})
    graph_doc.redirect_edges = [EdgeSpec(source="a", target="a")]
    run_doc = a_run_doc(graph_doc)
    service, probe = a_probed_service(run_doc)

    def fired_counts(budgets) -> Dict[str, int]:
        """`fired` per pair, with absent treated as zero so the pre-init state is not a mismatch."""
        if not budgets:
            return {}
        return {
            key: (value["fired"] if isinstance(value, dict) else value.fired)
            for key, value in budgets.items()
            if (value["fired"] if isinstance(value, dict) else value.fired)
        }

    lagging: List[tuple] = []

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        stored = json.loads(probe.stored_document("run-1"))["result"].get("redirect_budgets")
        on_disk = fired_counts(stored)
        in_memory = fired_counts(run_doc.result.redirect_budgets)
        if on_disk != in_memory:
            lagging.append((action_id, on_disk, in_memory))
        return an_action_result(action_id)

    with patch.object(ActionGraph, "run_action", AsyncMock(side_effect=fake_run_action)):
        final = await ActionGraph.run_action_graph_v2(
            ctx=Context(), app_state=AppState(), user_actions=AsyncMock(),
            action_graph_input_dict={"text": "in"}, action_graph_result_doc=run_doc,
            action_graph_node_id=None, is_continue_run_graph=False,
            user_action_graph_result=service, notify=None,
        )

    assert final.result.redirect_budgets["a|a"].fired > 0, "nothing fired, so this proves nothing"
    assert not lagging, (
        "a node started while a fired redirect was still only in memory; a crash here would lose "
        f"the count and let a retry re-fire it: {lagging}"
    )


@pytest.mark.asyncio
async def test_a_redirect_fired_on_a_skipped_node_is_persisted():
    """The skip path runs no node, so nothing saves after it unless it asks.

    On a resumed run *every* node is skipped, so an unpersisted increment there would never be
    written by anyone.
    """
    import json

    graph_doc = a_graph_doc({"s": ["a"], "a": ["b"]})
    graph_doc.redirect_edges = [EdgeSpec(source="a", target="a")]
    run_doc = a_run_doc(graph_doc)
    service, probe = a_probed_service(run_doc)

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        return an_action_result(action_id)

    async def attempt():
        with patch.object(ActionGraph, "run_action", AsyncMock(side_effect=fake_run_action)):
            return await ActionGraph.run_action_graph_v2(
                ctx=Context(), app_state=AppState(), user_actions=AsyncMock(),
                action_graph_input_dict={"text": "in"}, action_graph_result_doc=run_doc,
                action_graph_node_id=None, is_continue_run_graph=False,
                user_action_graph_result=service, notify=None,
            )

    await attempt()
    # Raise the limit, then resume: the forward pass is complete, so every node is skipped and the
    # only thing that happens is a redirect firing off the skip path.
    run_doc.result.redirect_budgets["a|a"] = run_doc.result.redirect_budgets["a|a"].model_copy(
        update={"limit": run_doc.result.redirect_budgets["a|a"].fired + 2}
    )
    final = await attempt()

    stored = json.loads(probe.stored_document("run-1"))
    assert stored["result"]["redirect_budgets"]["a|a"]["fired"] == (
        final.result.redirect_budgets["a|a"].fired
    ), "the resumed run's increments stayed in memory"


# --------------------------------------------------------------------------- the human gates


async def run_graph_with_flags(graph, *, redirect_edges=None, flags=None):
    """`flags` sets user_input/user_review fields on named nodes."""
    graph_doc = a_graph_doc(graph)
    graph_doc.redirect_edges = redirect_edges or []
    for node, updates in (flags or {}).items():
        for field, value in updates.items():
            setattr(graph_doc.node_details[node].data, field, value)
    run_doc = a_run_doc(graph_doc)
    service, _ = a_probed_service(run_doc)

    executed: List[str] = []

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        executed.append(action_id.removeprefix("action-"))
        assert len(executed) <= 200, "runaway"
        return an_action_result(action_id)

    with patch.object(ActionGraph, "run_action", AsyncMock(side_effect=fake_run_action)):
        final = await ActionGraph.run_action_graph_v2(
            ctx=Context(), app_state=AppState(), user_actions=AsyncMock(),
            action_graph_input_dict={"text": "in"}, action_graph_result_doc=run_doc,
            action_graph_node_id=None, is_continue_run_graph=False,
            user_action_graph_result=service, notify=None,
        )
    return final, executed


@pytest.mark.asyncio
async def test_a_dag_still_waits_on_a_node_that_needs_input():
    """The gates are unchanged for every graph without a redirect edge."""
    from src.autobots.event_result.event_result_model import EventResultStatus

    final, executed = await run_graph_with_flags(
        {START: ["a"], "a": ["b"]}, flags={"a": {"user_input_required": True}}
    )

    assert "a" not in executed, "the gate did not hold the node"
    assert "b" not in executed, "the gate must hold everything after it too"
    assert final.status == EventResultStatus.waiting


@pytest.mark.asyncio
async def test_a_dag_runs_once_the_input_is_marked_done():
    from src.autobots.event_result.event_result_model import EventResultStatus

    final, executed = await run_graph_with_flags(
        {START: ["a"], "a": ["b"]},
        flags={"a": {"user_input_required": True, "user_input_done": True}},
    )

    assert executed == [START, "a", "b"]
    assert final.status == EventResultStatus.success


@pytest.mark.asyncio
async def test_a_review_granted_on_the_forward_pass_does_not_re_gate_the_turns():
    """The point of the change.

    The review verdict lives on `node_details`, so it has no per-turn dimension. A re-entry can
    only ever read the baseline's verdict. Scoping the gate to the baseline says that plainly,
    instead of relying on the flag happening to stay `done`.
    """
    graph = {START: ["a"], "a": ["y"], "y": ["z"]}

    final, executed = await run_graph_with_flags(
        graph,
        redirect_edges=[EdgeSpec(source="z", target="a")],
        flags={"a": {"user_review_required": True, "user_review_done": True}},
    )

    assert executed.count("z") > 1, "the cycle never ran"
    assert executed.count("y") > 1, "the review gate blocked a turn it had already approved"


@pytest.mark.asyncio
async def test_a_gate_hit_on_the_forward_pass_still_parks_the_whole_run():
    """Scoping to the baseline must not weaken the gate where it belongs.

    `x` sits after `a` in rank order, so the gate fires mid-forward-pass. Nothing downstream of it
    may run, and the cycle must never start.
    """
    from src.autobots.event_result.event_result_model import EventResultStatus

    graph = {START: ["a"], "a": ["x"], "x": ["z"]}

    final, executed = await run_graph_with_flags(
        graph,
        redirect_edges=[EdgeSpec(source="z", target="a")],
        flags={"x": {"user_input_required": True}},
    )

    assert "x" not in executed
    assert "z" not in executed, "the cycle started behind a closed gate"
    assert final.status == EventResultStatus.waiting
