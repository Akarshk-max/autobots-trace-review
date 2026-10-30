"""`is_node_done` - the predicate that decides whether a node is skipped or run.

This is the single most load-bearing change in Part 3, because it sits on the path of *every* run,
DAG included. Two properties matter and both are tested against their wrong versions:

  1. At `K == 1` it must reduce to exactly "is the key present in `output`". If it does not, every
     workflow in production changes behaviour, not just cyclic ones.
  2. At `K > 1` it must be **re-armable** - a node done for one lap must not read as done for the
     next. Without that, "done" stays permanent and a cycle can never execute.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action_graph.action_graph.action_graph import ActionGraph
from src.autobots.action_graph.action_graph.action_graph_doc_model import ActionGraphDoc
from src.autobots.action_graph.action_graph.context_vector import BASELINE_SLOT
from src.autobots.action_graph.action_graph.node_result_writer import (
    NodeRunContext,
    forget_node,
    is_node_done,
    record_node_result,
)
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
)
from src.autobots.event_result.event_result_model import EventResultStatus, EventType

START = "s"
SLOTS = (BASELINE_SLOT, "1", "2")


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
    return ActionGraphResultDoc(
        _id="run-1",
        user_id="user-1",
        type=EventType.action_graph_run,
        status=EventResultStatus.processing,
        result=ActionGraphDoc(
            _id="graph-1",
            user_id="user-1",
            name="g",
            graph={"s": ["a"], "a": ["b"]},
            output={},
        ),
    )


def cyclic_ctx(node: str, *, tag: str = START, turn_no: int = 0) -> NodeRunContext:
    return NodeRunContext(
        node=node, start_node_id=START, tag=tag, turn_no=turn_no, target_slots=SLOTS
    )


# --------------------------------------------------------------------------- K == 1


def test_a_dag_reduces_to_key_presence():
    """The property every existing workflow depends on."""
    run = a_run()
    assert not is_node_done(run, "a"), "nothing has run"

    record_node_result(run, an_action_doc(), NodeRunContext.for_dag("a"))

    assert is_node_done(run, "a")
    assert not is_node_done(run, "b"), "a different node is unaffected"
    assert run.result.node_vectors is None, "a DAG still carries no vector"


def test_a_dag_ignores_the_context_index_entirely():
    """With no vector there are no slots, so asking about one cannot change the answer.

    If this ever fails, a DAG's skip decisions have started depending on a context it does not
    have - which is the exact regression that would break every non-cyclic workflow.
    """
    run = a_run()
    record_node_result(run, an_action_doc(), NodeRunContext.for_dag("a"))

    for slot in (BASELINE_SLOT, "1", "7", "nonexistent"):
        assert is_node_done(run, "a", context_index=slot)


def test_forgetting_a_node_makes_it_not_done_again():
    """The rerun path depends on this: `empty_downstream_nodes` must actually re-arm a node."""
    run = a_run()
    record_node_result(run, an_action_doc(), NodeRunContext.for_dag("a"))
    assert is_node_done(run, "a")

    forget_node(run, "a")

    assert not is_node_done(run, "a")


# --------------------------------------------------------------------------- K > 1


def test_a_populated_slot_reads_as_done_for_that_context():
    run = a_run()
    record_node_result(run, an_action_doc(), cyclic_ctx("a"))

    assert is_node_done(run, "a", context_index=BASELINE_SLOT)
    assert is_node_done(run, "a", context_index="1")


def test_an_unwritten_slot_reads_as_not_done():
    """Case (i) fills every slot, so this is the shape where only *some* slots were targeted."""
    run = a_run()
    record_node_result(
        run,
        an_action_doc(),
        NodeRunContext(node="a", start_node_id=START, tag="1", turn_no=0, target_slots=("1",)),
    )

    assert is_node_done(run, "a", context_index="1")
    assert not is_node_done(run, "a", context_index="2"), "slot 2 was never written"


def test_output_without_a_vector_reads_as_not_done():
    """Drift: a cyclic run whose node was written with `for_dag`.

    Answering "done" would silently skip a node that recorded no context. Answering "not done"
    re-runs it, which is the recoverable direction; `assert_vector_covers_output` reports the drift.
    """
    run = a_run()
    record_node_result(run, an_action_doc(), cyclic_ctx("a"))  # makes node_vectors a dict
    record_node_result(run, an_action_doc(), NodeRunContext.for_dag("b"))  # output only

    assert run.result.node_vectors is not None
    assert "b" not in run.result.node_vectors
    assert not is_node_done(run, "b")


# --------------------------------------------------------------------------- re-armability


def test_a_new_turn_re_arms_the_node():
    """The heart of it. Lap 1 being done must not make lap 2 look done."""
    run = a_run()
    record_node_result(run, an_action_doc(), cyclic_ctx("a", tag="1", turn_no=0))

    assert is_node_done(run, "a", context_index="1", tag="1", turn_no=0), "lap 0 is recorded"
    assert not is_node_done(run, "a", context_index="1", tag="1", turn_no=1), (
        "lap 1 has not run - if this reads as done, the node can never be re-entered"
    )

    record_node_result(run, an_action_doc(), cyclic_ctx("a", tag="1", turn_no=1))

    assert is_node_done(run, "a", context_index="1", tag="1", turn_no=1)


def test_a_different_tag_at_the_same_turn_is_a_different_execution():
    """Turn counters run per (slot, tag), so the tag is part of the identity."""
    run = a_run()
    record_node_result(run, an_action_doc(), cyclic_ctx("a", tag="1", turn_no=0))

    assert is_node_done(run, "a", context_index="1", tag="1", turn_no=0)
    assert not is_node_done(run, "a", context_index="1", tag="2", turn_no=0), (
        "a redirect from a different node is a different event"
    )


def test_the_coarse_reading_ignores_which_execution():
    """Upstream readiness asks 'has anything landed here', not 'has this exact lap landed'."""
    run = a_run()
    record_node_result(run, an_action_doc(), cyclic_ctx("a", tag="1", turn_no=3))

    assert is_node_done(run, "a", context_index="1"), "something is in the slot"
    assert not is_node_done(run, "a", context_index="1", tag="1", turn_no=0), (
        "but not that particular lap"
    )


def test_tag_and_turn_must_be_given_together():
    """Half an identity would silently never match, which reads as 'not done' forever."""
    run = a_run()
    record_node_result(run, an_action_doc(), cyclic_ctx("a"))

    with pytest.raises(ValueError, match="both or neither"):
        is_node_done(run, "a", tag="1")
    with pytest.raises(ValueError, match="both or neither"):
        is_node_done(run, "a", turn_no=0)


# --------------------------------------------------------------------------- the engine wrapper


@pytest.mark.asyncio
async def test_is_work_done_requires_every_node():
    run = a_run()
    record_node_result(run, an_action_doc(), NodeRunContext.for_dag("a"))

    assert await ActionGraph.is_work_done(["a"], run)
    assert not await ActionGraph.is_work_done(["a", "b"], run)
    assert await ActionGraph.is_work_done([], run), "no upstreams means nothing to wait for"


@pytest.mark.asyncio
async def test_is_work_done_tolerates_no_upstreams():
    """A root node's upstream list can arrive empty; it must read as satisfied, not crash."""
    run = a_run()
    assert await ActionGraph.is_work_done(None, run)
