"""Tests for the single node-result write point and the baseline-mirror invariant.

`result.output[node]` and `result.node_vectors[node]` hold the same execution in two shapes. Every
reader touches only the first, so a missed vector write is invisible until the scheduler reads a
slot and gets the wrong context. These tests are the guard.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

import pytest

from src.autobots.action.action.action_doc_model import ActionDoc, ActionResult
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action_graph.action_graph.action_graph_doc_model import ActionGraphDoc
from src.autobots.action_graph.action_graph.context_vector import (
    BASELINE_SLOT,
    ContextSlot,
    append_entry,
    baseline_matches_action_doc,
    merge_slots,
    upsert_entry,
)
from src.autobots.action_graph.action_graph.node_result_writer import (
    NodeRunContext,
    assert_baseline_mirror,
    assert_vector_covers_output,
    forget_all,
    forget_node,
    record_node_result,
)
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
)
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus, EventType

START = "s"
CYCLIC_SLOTS = (BASELINE_SLOT, "1", "2")


def an_action_doc(**overrides: Any) -> ActionDoc:
    payload: Dict[str, Any] = {
        "_id": "action-1",
        "user_id": "user-1",
        "name": "an action",
        "type": ActionType.mock_action,
        "input": {"text": "in"},
        "output": {"blocks": [{"text": "out"}]},
    }
    payload.update(overrides)
    return ActionDoc(**payload)


def a_run() -> ActionGraphResultDoc:
    graph = ActionGraphDoc(
        _id="graph-1",
        user_id="user-1",
        name="g",
        graph={"s": ["a"], "a": ["b"]},
        output={},
    )
    return ActionGraphResultDoc(
        _id="run-1",
        user_id="user-1",
        type=EventType.action_graph_run,
        status=EventResultStatus.processing,
        result=graph,
    )


def cyclic_ctx(node: str, **overrides: Any) -> NodeRunContext:
    fields: Dict[str, Any] = {
        "node": node,
        "start_node_id": START,
        "target_slots": CYCLIC_SLOTS,
    }
    fields.update(overrides)
    return NodeRunContext(**fields)


# --------------------------------------------------------------------------- the DAG path


def test_a_dag_write_leaves_node_vectors_absent():
    """K == 1 must stay byte-identical to a run produced before cyclic support existed."""
    run = a_run()

    record_node_result(run, an_action_doc(), NodeRunContext.for_dag("a"))

    assert run.result.output["a"].output == {"blocks": [{"text": "out"}]}
    assert run.result.node_vectors is None


def test_a_dag_write_stores_the_whole_executable():
    run = a_run()

    record_node_result(run, an_action_doc(config={"model": "gpt"}), NodeRunContext.for_dag("a"))

    stored = run.result.output["a"]
    assert (stored.type, stored.config, stored.name) == (
        ActionType.mock_action,
        {"model": "gpt"},
        "an action",
    )


# --------------------------------------------------------------------------- the cyclic path


def test_a_cyclic_write_mirrors_into_every_target_slot():
    """Case (i): nothing has redirected yet, so every dimension starts from the same place."""
    run = a_run()

    record_node_result(run, an_action_doc(), cyclic_ctx("a"))

    vector = run.result.node_vectors["a"]
    assert sorted(vector.slots) == list(CYCLIC_SLOTS)
    assert baseline_matches_action_doc(vector, run.result.output["a"], start_node_id=START)


def test_the_forward_pass_is_tagged_with_the_start_node():
    run = a_run()

    record_node_result(run, an_action_doc(), cyclic_ctx("a"))

    entry = run.result.node_vectors["a"].baseline.entries[0]
    assert (entry.node_id, entry.turn_no) == (START, 0)


def test_a_re_entry_is_tagged_with_the_redirecting_node():
    run = a_run()
    record_node_result(run, an_action_doc(), cyclic_ctx("a"))

    record_node_result(
        run,
        an_action_doc(output={"blocks": [{"text": "lap 2"}]}),
        cyclic_ctx("a", tag="2", turn_no=0, target_slots=("1",)),
    )

    slot = run.result.node_vectors["a"].slot("1")
    assert [(e.node_id, e.turn_no) for e in slot.sorted_entries(START)] == [(START, 0), ("2", 0)]
    # The dimension that was not targeted is untouched.
    assert [e.node_id for e in run.result.node_vectors["a"].slot("2").entries] == [START]


def test_the_tag_is_never_the_node_being_executed():
    """The entry records where the context came from, not where it landed."""
    run = a_run()

    record_node_result(run, an_action_doc(), cyclic_ctx("a"))
    record_node_result(
        run, an_action_doc(), cyclic_ctx("a", tag="2", target_slots=("1",))
    )

    tags = {e.node_id for slot in run.result.node_vectors["a"].slots.values() for e in slot.entries}
    assert tags == {START, "2"}
    assert "a" not in tags


def test_a_slot_write_without_a_provenance_tag_is_refused():
    """Otherwise the entry is tagged "" - a provenance no slot lookup would ever match."""
    with pytest.raises(ValueError, match="provenance tag"):
        NodeRunContext(node="a", target_slots=(BASELINE_SLOT,))


def test_a_dag_context_needs_no_tag():
    """It writes no slots, so there is no provenance to record."""
    assert NodeRunContext.for_dag("a").provenance_tag == ""


# --------------------------------------------------------------------------- progressive loops


def test_a_loop_node_reporting_progressively_leaves_exactly_one_entry():
    """The engine records a loop node once per iteration with a bigger collated doc each time.

    That whole fan-out is ONE entry into the node. If this ever asserts 3, someone has swapped the
    writer's upsert for an append and multiplied every loop node's payload by its iteration count.
    """
    run = a_run()
    ctx = cyclic_ctx("a")

    for i in range(1, 4):
        collated = an_action_doc(
            output={"blocks": [{"text": f"row {n}"} for n in range(1, i + 1)]},
            results=[{"input": {"text": f"row {n}"}, "output": {}} for n in range(1, i + 1)],
        )
        record_node_result(run, collated, ctx)

    slot = run.result.node_vectors["a"].baseline
    assert len(slot.entries) == 1
    # ...and it holds the FINAL collation, not the first.
    assert slot.entries[0].output == {"blocks": [{"text": "row 1"}, {"text": "row 2"}, {"text": "row 3"}]}
    assert len(run.result.output["a"].results) == 3, "the ActionDoc keeps the per-iteration record"


def test_the_finer_grained_record_stays_on_the_action_doc():
    """The dashboard counts len(output[node].results) as rows processed; the vector cannot say."""
    run = a_run()
    collated = an_action_doc(results=[{"input": {}, "output": {}} for _ in range(200)])

    record_node_result(run, collated, cyclic_ctx("a"))

    assert len(run.result.output["a"].results) == 200
    assert len(run.result.node_vectors["a"].baseline.entries) == 1


def test_a_placeholder_is_replaced_not_appended():
    """run_single_node writes a pending placeholder, then the real result into the same cell."""
    run = a_run()
    ctx = cyclic_ctx("a")

    record_node_result(run, an_action_doc(output={}), ctx)
    record_node_result(run, an_action_doc(output={"blocks": [{"text": "real"}]}), ctx)

    slot = run.result.node_vectors["a"].baseline
    assert len(slot.entries) == 1
    assert slot.entries[0].output == {"blocks": [{"text": "real"}]}


# --------------------------------------------------------------------------- single-node rerun


@pytest.mark.asyncio
async def test_a_single_node_rerun_stores_the_real_result_not_the_placeholder():
    """`run_node` discards the return value, so the stored doc is the only record of the rerun.

    This used to work by aliasing - `output[node]` *was* `node_action_doc`, which `run_action_doc`
    mutates in place. Recording a snapshot instead broke that, leaving the pending placeholder
    stored and the real output nowhere.
    """
    from unittest.mock import AsyncMock, patch

    from src.autobots.action_graph.action_graph.action_graph import ActionGraph

    run = a_run()
    record_node_result(run, an_action_doc(output={"blocks": []}), NodeRunContext.for_dag("a"))

    async def fake_run_action_doc(*, action_doc, **kwargs):
        # What the real one does: mutate in place and hand the same object back.
        action_doc.output = {"blocks": [{"text": "the real output"}]}
        action_doc.results.append(ActionResult(input={"text": "in"}, output=action_doc.output))
        return action_doc

    with patch.object(ActionGraph, "run_action_doc", AsyncMock(side_effect=fake_run_action_doc)):
        await ActionGraph.run_node_action_with_existing_doc(
            ctx=Context(),
            app_state=None,
            node="a",
            action_graph_input_dict={"text": "in"},
            action_graph_result_doc=run,
            run_ctx=NodeRunContext.for_dag("a"),
            user_actions=AsyncMock(),
            user_action_graph_result=AsyncMock(),
            run_single_node=True,
        )

    stored = run.result.output["a"]
    assert stored.output == {"blocks": [{"text": "the real output"}]}
    assert all(r.output is not None for r in stored.results), "a pending placeholder was left behind"


def test_record_node_result_stores_by_reference_not_by_copy():
    """An undocumented dependency worth pinning.

    `ActionDoc.model_validate(doc)` returns the *same object* when `doc` is already an ActionDoc,
    because pydantic's `revalidate_instances` defaults to "never". `run_node` relies on that: it
    discards the return value of `run_node_action_with_existing_doc` and expects `output[node]` to
    pick up the in-place mutations `run_action_doc` makes. If this ever fails, that rerun path
    silently starts saving the pending placeholder instead of the result.
    """
    run = a_run()
    doc = an_action_doc(output={})

    stored = record_node_result(run, doc, NodeRunContext.for_dag("a"))

    assert stored is doc
    assert run.result.output["a"] is doc

    doc.output = {"blocks": [{"text": "mutated after the write"}]}
    assert run.result.output["a"].output == {"blocks": [{"text": "mutated after the write"}]}


# --------------------------------------------------------------------------- deletion


def test_forget_node_clears_both_structures():
    run = a_run()
    record_node_result(run, an_action_doc(), cyclic_ctx("a"))
    record_node_result(run, an_action_doc(), cyclic_ctx("b"))

    forget_node(run, "a")

    assert set(run.result.output) == {"b"}
    assert set(run.result.node_vectors) == {"b"}


def test_forget_all_clears_both_structures():
    run = a_run()
    record_node_result(run, an_action_doc(), cyclic_ctx("a"))

    forget_all(run)

    assert run.result.output == {}
    assert run.result.node_vectors == {}


def test_forget_all_leaves_a_dag_run_without_vectors():
    run = a_run()
    record_node_result(run, an_action_doc(), NodeRunContext.for_dag("a"))

    forget_all(run)

    assert run.result.output == {}
    assert run.result.node_vectors is None


# --------------------------------------------------------------------------- the guards


def test_the_per_node_guard_notices_drift():
    run = a_run()
    record_node_result(run, an_action_doc(), cyclic_ctx("a"))
    assert assert_baseline_mirror(Context(), run, "a", start_node_id=START)

    # Simulate a write that touched only the ActionDoc.
    run.result.output["a"].output = {"blocks": [{"text": "drifted"}]}

    assert not assert_baseline_mirror(Context(), run, "a", start_node_id=START)


def test_the_per_node_guard_can_raise_under_strict():
    run = a_run()
    record_node_result(run, an_action_doc(), cyclic_ctx("a"))
    run.result.output["a"].output = {"blocks": [{"text": "drifted"}]}

    with pytest.raises(AssertionError):
        assert_baseline_mirror(Context(), run, "a", start_node_id=START, strict=True)


def test_the_per_node_guard_is_a_no_op_for_a_dag():
    run = a_run()
    record_node_result(run, an_action_doc(), NodeRunContext.for_dag("a"))

    assert assert_baseline_mirror(Context(), run, "a", start_node_id=START)


def test_the_coverage_guard_notices_a_one_sided_delete():
    """Deletion drift the per-node check structurally cannot see - there is nothing left to compare."""
    run = a_run()
    record_node_result(run, an_action_doc(), cyclic_ctx("a"))
    assert assert_vector_covers_output(Context(), run)

    run.result.output.pop("a")  # popped from one structure only

    assert not assert_vector_covers_output(Context(), run)
    assert assert_baseline_mirror(Context(), run, "a", start_node_id=START), (
        "the per-node check cannot catch this, which is why the coverage check exists"
    )


# --------------------------------------------------------------------------- the real guard


def test_no_direct_node_result_writes_outside_the_writer():
    """The failure mode is someone adding another write site in six months.

    The runtime guards only *detect* drift; this is the one that prevents it. It previously had two
    blind spots that let the production Temporal path through: it scanned only `action_graph/`, and
    its pattern matched subscripted writes but not whole-dict assignment (`.output = {}`).
    """
    repo = Path(__file__).resolve().parents[2]          # not CWD - passes vacuously elsewhere
    roots = [
        repo / "src/autobots/action_graph",
        repo / "src/autobots/conn/temporal/action_graph",   # the Temporal twin of the engine
    ]
    # Both shapes: `x.result.output[node] = ...` and `x.result.output = {}`.
    direct_write = re.compile(r"\.result\.(output|node_vectors)\s*(\[[^\]]+\])?\s*=(?!=)")
    allowed = {"node_result_writer.py"}

    scanned, offenders = 0, []
    for root in roots:
        assert root.is_dir(), f"guard is scanning nothing: {root}"
        for path in sorted(root.rglob("*.py")):
            if path.name in allowed or "action_graph_golds" in path.parts:
                continue
            # `*_prev.py` are frozen copies of the pre-cyclic engine, kept for reference and
            # imported by nothing. They are full of the direct writes this guard exists to outlaw,
            # which is the point of them - they record what the code used to do. Scanning an
            # archive tells us nothing about the code that runs.
            if path.stem.endswith("_prev"):
                continue
            scanned += 1
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if direct_write.search(line):
                    offenders.append(f"{path.relative_to(repo)}:{lineno}: {line.strip()}")

    assert scanned > 0, "guard scanned no files"
    assert not offenders, (
        "Write node results via record_node_result()/forget_node()/forget_all() so the context "
        "vector stays in step:\n" + "\n".join(offenders)
    )


# --------------------------------------------------------------------------- upsert vs append


def test_upsert_replaces_where_append_would_add():
    slot = ContextSlot()
    slot = upsert_entry(slot, node_id=START, turn_no=0, output={"v": 1}, start_node_id=START)
    slot = upsert_entry(slot, node_id=START, turn_no=0, output={"v": 2}, start_node_id=START)

    assert len(slot.entries) == 1 and slot.entries[0].output == {"v": 2}

    appended = append_entry(ContextSlot(), node_id=START, start_node_id=START)
    appended = append_entry(appended, node_id=START, start_node_id=START)
    assert len(appended.entries) == 2, "append_entry is for genuinely distinct turns"


def test_upsert_is_needed_because_merge_keeps_the_stale_value():
    """merge_slots is existing-wins, so it would keep a loop's *first* partial collation."""
    first = ContextSlot(entries=[])
    first = upsert_entry(first, node_id=START, turn_no=0, output={"rows": 1}, start_node_id=START)
    later = ContextSlot(entries=[])
    later = upsert_entry(later, node_id=START, turn_no=0, output={"rows": 3}, start_node_id=START)

    assert merge_slots(first, later, start_node_id=START).entries[0].output == {"rows": 1}
    assert upsert_entry(
        first, node_id=START, turn_no=0, output={"rows": 3}, start_node_id=START
    ).entries[0].output == {"rows": 3}


def test_forget_all_keeps_the_map_identity_so_a_held_binding_stays_live():
    """The aliasing hazard, closed structurally rather than by call ordering.

    The engine binds `node_results(doc)` once per function. If `forget_all` rebound
    `graph.output` to a fresh dict, that binding would point at a detached map: `is_work_done`
    would read results that had been cleared, and nodes would be wrongly skipped. Today every
    `forget_all` happens before the engine takes its binding - but Part 3 re-enters nodes mid-run,
    and this must not depend on that order.
    """
    run = a_run()
    record_node_result(run, an_action_doc(), cyclic_ctx("a"))

    held_output = run.result.output
    held_vectors = run.result.node_vectors

    forget_all(run)

    assert run.result.output is held_output, "a held binding was detached from the document"
    assert run.result.node_vectors is held_vectors
    assert held_output == {} and held_vectors == {}

    # And a write after the reset lands where the held binding can see it.
    record_node_result(run, an_action_doc(), cyclic_ctx("b"))
    assert set(held_output) == {"b"}
