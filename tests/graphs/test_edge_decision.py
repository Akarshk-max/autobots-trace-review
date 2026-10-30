"""Per-`(edge, turn)` verdicts - OQ-9's storage, and the one question it answers.

Storage only in this branch. What these tests pin is the shape, and above all the **default**:
an edge nobody asked contributes exactly as it does today. Nothing writes a verdict until Part 5,
so if absence read as "declined" every graph in production would lose every parent at once.
"""

from __future__ import annotations

import pytest

from src.autobots.action_graph.action_graph.action_graph_doc_model import ActionGraphDoc
from src.autobots.action_graph.action_graph.context_vector import upsert_entry
from src.autobots.action_graph.action_graph.edge_decision import (
    EdgeDecision,
    contributing_parents,
    decision_for,
    decision_key,
    declined_parents,
    has_declined,
    record_decision,
    split_key,
)

LAP = dict(context_index="1", turn_no=2)


# --------------------------------------------------------------------------- the default


def test_an_edge_nobody_asked_has_not_declined():
    """Absence is "no verdict", not "no". This is the property every graph today depends on."""
    assert not has_declined(None, "child", "parent", **LAP)
    assert not has_declined({}, "child", "parent", **LAP)
    assert declined_parents(None, "child", ["p1", "p2"], **LAP) == []
    assert contributing_parents(None, "child", ["p1", "p2"], **LAP) == ["p1", "p2"]


def test_the_parent_order_is_the_order_it_was_given():
    """Upstream blocks appear in the composed prompt in this order, so it is not a set."""
    decisions = record_decision(None, "child", "p2", fired=False, **LAP)

    assert contributing_parents(decisions, "child", ["p3", "p1", "p2"], **LAP) == ["p3", "p1"]


# --------------------------------------------------------------------------- recording a verdict


def test_a_declining_edge_removes_only_that_parent():
    decisions = record_decision(
        None, "child", "p1", fired=False, reason="nothing new to add", **LAP
    )

    assert has_declined(decisions, "child", "p1", **LAP)
    assert not has_declined(decisions, "child", "p2", **LAP)
    assert declined_parents(decisions, "child", ["p1", "p2"], **LAP) == ["p1"]
    assert contributing_parents(decisions, "child", ["p1", "p2"], **LAP) == ["p2"]


def test_an_edge_that_fired_still_contributes():
    """`fired=True` is a recorded *yes*, not a recorded absence."""
    decisions = record_decision(None, "child", "p1", fired=True, **LAP)

    assert not has_declined(decisions, "child", "p1", **LAP)
    assert contributing_parents(decisions, "child", ["p1"], **LAP) == ["p1"]


def test_the_reason_is_kept_for_the_account_it_gives():
    """RISK-9: a surprising path has to be explainable afterwards."""
    decisions = record_decision(
        None, "child", "p1", fired=False, reason="the draft already passes review", **LAP
    )

    assert decision_for(decisions, "child", "p1", **LAP).reason == (
        "the draft already passes review"
    )


def test_recording_does_not_mutate_what_it_was_given():
    before = {}

    after = record_decision(before, "child", "p1", fired=False, **LAP)

    assert before == {}, "the input map was mutated"
    assert after != before


# --------------------------------------------------------------------------- what the key separates


def test_one_node_can_answer_differently_for_each_edge_leaving_it():
    """D9: the node runs once. The verdict is per edge, which is why it cannot live on the node."""
    decisions = record_decision(None, "a", "z", fired=True, **LAP)
    decisions = record_decision(decisions, "b", "z", fired=False, **LAP)

    assert not has_declined(decisions, "a", "z", **LAP)
    assert has_declined(decisions, "b", "z", **LAP)


def test_one_edge_can_answer_differently_on_each_turn():
    """The point of a *cycle*: the second time round, the answer may be no."""
    decisions = record_decision(None, "a", "z", context_index="1", turn_no=0, fired=True)
    decisions = record_decision(decisions, "a", "z", context_index="1", turn_no=1, fired=False)

    assert not has_declined(decisions, "a", "z", context_index="1", turn_no=0)
    assert has_declined(decisions, "a", "z", context_index="1", turn_no=1)


def test_the_same_turn_in_two_columns_is_two_different_turns():
    """Turns count per `(slot, tag)`, so `turn_no` alone is ambiguous across columns."""
    decisions = record_decision(None, "a", "z", context_index="1", turn_no=0, fired=False)

    assert has_declined(decisions, "a", "z", context_index="1", turn_no=0)
    assert not has_declined(decisions, "a", "z", context_index="2", turn_no=0), (
        "a verdict leaked between columns"
    )


def test_keys_round_trip_and_stay_mongo_safe():
    key = decision_key("node-a", "node-z", 1, 3)

    assert key == "node-a|node-z|1|3"
    assert split_key(key) == ("node-a", "node-z", "1", 3)
    assert "." not in key and not key.startswith("$")


def test_an_int_slot_and_its_string_key_are_the_same_turn():
    """`GraphTopology.context_index` hands out ints; vector slots are keyed by str."""
    decisions = record_decision(None, "a", "z", context_index=1, turn_no=0, fired=False)

    assert has_declined(decisions, "a", "z", context_index="1", turn_no=0)


# --------------------------------------------------------------------------- where it does not live


def test_a_verdict_stored_on_the_context_entry_would_not_survive_a_loop_node():
    """Why this is not a field on `ContextEntry`, demonstrated rather than asserted in prose.

    A loop node reports progressively - `record_node_result` once per iteration, into the same
    `(turn_no, node_id)` cell - and `upsert_entry` replaces that cell every time. Anything written
    alongside the entry is gone by the node's own second iteration.
    """
    from src.autobots.action_graph.action_graph.context_vector import ContextSlot

    slot = ContextSlot()
    slot = upsert_entry(slot, node_id="z", turn_no=0, output={"verdict": "declined p1"})
    # The loop's next iteration writes the same cell with the collation so far.
    slot = upsert_entry(slot, node_id="z", turn_no=0, output={"blocks": ["row 1", "row 2"]})

    assert len(slot.entries) == 1
    assert slot.entries[0].output == {"blocks": ["row 1", "row 2"]}, (
        "the cell was replaced, as it must be - so it is not a home for a verdict"
    )


# --------------------------------------------------------------------------- the document


def test_a_dag_carries_no_decision_map():
    """`None`, never `{}` - the same discipline as `node_vectors` and `redirect_budgets`."""
    doc = ActionGraphDoc(_id="g", user_id="u", name="g", graph={"s": ["a"]})

    assert doc.edge_decisions is None


def test_the_map_round_trips_through_the_document():
    doc = ActionGraphDoc(
        _id="g",
        user_id="u",
        name="g",
        graph={"s": ["a"]},
        edge_decisions=record_decision(None, "a", "s", fired=False, reason="why", **LAP),
    )

    reloaded = ActionGraphDoc.model_validate(doc.model_dump(by_alias=True))

    assert reloaded.edge_decisions == {
        "a|s|1|2": EdgeDecision(fired=False, reason="why")
    }


@pytest.mark.parametrize("fired", [True, False])
def test_fired_has_no_default(fired):
    """A verdict with no answer in it is not a verdict; the caller must say which way."""
    assert EdgeDecision(fired=fired).fired is fired
    with pytest.raises(ValueError):
        EdgeDecision()  # type: ignore[call-arg]
