"""Tests for the context vector - the per-redirector context slots and the f_comb merge."""

from __future__ import annotations

from typing import Any, Dict

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action_graph.action_graph.action_graph_doc_model import (
    ActionGraphDoc,
    ActionGraphUpdate,
)
from src.autobots.action_graph.action_graph.context_vector import (
    BASELINE_SLOT,
    ContextEntry,
    ContextSlot,
    append_entry,
    baseline_matches_action_doc,
    baseline_vector,
    entry_from_action_doc,
    merge_slots,
    seed_all_slots,
    sort_key,
)

# Node ids are digit strings; the start node's is a letter. That is what makes the explicit
# start-first rank in the sort key necessary.
START = "s"


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


def entry(node_id: str, turn_no: int, text: str = "") -> ContextEntry:
    return ContextEntry(
        input={"text": text}, output={"blocks": []}, node_id=node_id, turn_no=turn_no
    )


def order(slot: ContextSlot) -> list[tuple[str, int]]:
    return [(e.node_id, e.turn_no) for e in slot.sorted_entries(START)]


# --------------------------------------------------------------------------- ordering


def test_start_leads_within_a_turn_despite_sorting_after_digits_lexicographically():
    assert "s" > "2", "precondition: a letter sorts after a digit"

    slot = ContextSlot(entries=[entry("2", 0), entry("5", 0), entry(START, 0)])

    assert order(slot) == [(START, 0), ("2", 0), ("5", 0)]


def test_ordering_is_chronological_across_turns():
    slot = ContextSlot(entries=[entry("2", 1), entry(START, 0), entry("2", 0), entry("5", 0)])

    assert order(slot) == [(START, 0), ("2", 0), ("5", 0), ("2", 1)]


def test_sort_key_ranks_only_the_start_tag_first():
    assert sort_key(entry(START, 0), START) == (0, 0, START)
    assert sort_key(entry("2", 0), START) == (0, 1, "2")


def test_ordering_survives_a_missing_start_node_id():
    """A malformed graph has no single root, so nothing is ranked first - but it still sorts."""
    slot = ContextSlot(entries=[entry("5", 0), entry("2", 0)])

    assert [(e.node_id, e.turn_no) for e in slot.sorted_entries(None)] == [("2", 0), ("5", 0)]


# --------------------------------------------------------------------------- turn numbers


def test_turn_counters_advance_per_tag_independently():
    slot = ContextSlot(entries=[entry(START, 0), entry("2", 0), entry("2", 1)])

    assert slot.next_turn_no("2") == 2
    assert slot.next_turn_no("5") == 0, "an unseen tag starts at 0, not after the others"
    assert slot.next_turn_no(START) == 1


def test_a_transcript_may_be_sparse_in_turns():
    """No global lap number: tag "2" reaching turn 2 does not require the start tag to follow."""
    slot = ContextSlot(entries=[entry(START, 0)])
    for _ in range(3):
        slot = append_entry(slot, node_id="2", start_node_id=START)

    assert order(slot) == [(START, 0), ("2", 0), ("2", 1), ("2", 2)]
    assert len(slot.tagged(START)) == 1


def test_append_takes_the_next_turn_for_its_tag():
    slot = append_entry(ContextSlot(), node_id="2", start_node_id=START)
    slot = append_entry(slot, node_id="2", start_node_id=START)

    assert [e.turn_no for e in slot.tagged("2")] == [0, 1]


# --------------------------------------------------------------------------- f_comb


def test_merge_is_pure_addition():
    existing = ContextSlot(entries=[entry(START, 0), entry("2", 0)])
    incoming = ContextSlot(entries=[entry("5", 0), entry("2", 1)])

    merged = merge_slots(existing, incoming, start_node_id=START)

    assert order(merged) == [(START, 0), ("2", 0), ("5", 0), ("2", 1)]


def test_merge_never_overwrites_an_existing_turn():
    """A redirect does not erase what the node already knew along its forward path."""
    existing = ContextSlot(entries=[entry("2", 0, text="original")])
    incoming = ContextSlot(entries=[entry("2", 0, text="replacement")])

    merged = merge_slots(existing, incoming, start_node_id=START)

    assert len(merged.entries) == 1
    assert merged.entries[0].input == {"text": "original"}


def test_merge_drops_duplicates_by_turn_and_tag():
    slot = ContextSlot(entries=[entry("2", 0), entry(START, 0)])

    merged = merge_slots(slot, slot, start_node_id=START)

    assert order(merged) == [(START, 0), ("2", 0)]


def test_merge_leaves_the_inputs_untouched():
    existing = ContextSlot(entries=[entry(START, 0)])
    incoming = ContextSlot(entries=[entry("2", 0)])

    merge_slots(existing, incoming, start_node_id=START)

    assert len(existing.entries) == 1 and len(incoming.entries) == 1


def test_merging_an_empty_slot_is_a_no_op():
    slot = ContextSlot(entries=[entry(START, 0)])

    assert merge_slots(slot, ContextSlot(), start_node_id=START).entries == slot.entries
    assert merge_slots(ContextSlot(), slot, start_node_id=START).entries == slot.entries


# --------------------------------------------------------------------------- vectors


def test_a_dag_node_carries_only_the_baseline():
    vector = baseline_vector(an_action_doc(), start_node_id=START)

    assert list(vector.slots) == [BASELINE_SLOT]
    assert vector.baseline.entries[0].node_id == START
    assert vector.baseline.entries[0].turn_no == 0


def test_case_one_writes_the_same_result_into_every_slot():
    """No redirection has happened yet, so there is nothing to disambiguate."""
    vector = seed_all_slots(an_action_doc(), start_node_id=START, slot_keys=[1, 2])

    assert sorted(vector.slots) == ["0", "1", "2"]
    payloads = [(s.entries[0].input, s.entries[0].output) for s in vector.slots.values()]
    assert len(set(map(str, payloads))) == 1, "every dimension starts from the same place"


def test_case_one_slots_do_not_share_entry_objects():
    """They start equal but diverge independently once redirects begin."""
    vector = seed_all_slots(an_action_doc(), start_node_id=START, slot_keys=[1])

    assert vector.slots["0"].entries[0] is not vector.slots["1"].entries[0]


def test_reading_an_absent_slot_is_empty_not_an_error():
    vector = baseline_vector(an_action_doc(), start_node_id=START)

    assert vector.slot(7).entries == []


def test_with_slot_returns_a_new_vector():
    vector = baseline_vector(an_action_doc(), start_node_id=START)
    updated = vector.with_slot(1, ContextSlot(entries=[entry("2", 0)]))

    assert "1" not in vector.slots
    assert sorted(updated.slots) == ["0", "1"]


# --------------------------------------------------------------------------- baseline projection


def test_one_execution_becomes_one_entry_even_for_a_loop_node():
    """A loop node collates many parallel runs into one ActionDoc - still one entry into the node."""
    looped = an_action_doc(
        results=[
            {"input": {"text": "a"}, "output": {"blocks": []}},
            {"input": {"text": "b"}, "output": {"blocks": []}},
            {"input": {"text": "c"}, "output": {"blocks": []}},
        ]
    )

    vector = baseline_vector(looped, start_node_id=START)

    assert len(vector.baseline.entries) == 1
    assert vector.baseline.entries[0].output == looped.output


def test_entry_carries_the_action_docs_input_and_output():
    doc = an_action_doc()
    e = entry_from_action_doc(doc, node_id=START)

    assert (e.input, e.output) == (doc.input, doc.output)


def test_baseline_projection_tracks_the_action_doc():
    doc = an_action_doc()
    vector = baseline_vector(doc, start_node_id=START)

    assert baseline_matches_action_doc(vector, doc, start_node_id=START)
    assert not baseline_matches_action_doc(
        vector, an_action_doc(output={"blocks": [{"text": "different"}]}), start_node_id=START
    )


def test_baseline_projection_follows_the_latest_turn():
    doc = an_action_doc()
    vector = baseline_vector(doc, start_node_id=START)
    rerun = an_action_doc(output={"blocks": [{"text": "second pass"}]})
    vector = vector.with_slot(
        BASELINE_SLOT,
        append_entry(
            vector.baseline,
            node_id=START,
            input=rerun.input,
            output=rerun.output,
            start_node_id=START,
        ),
    )

    assert baseline_matches_action_doc(vector, rerun, start_node_id=START)


# --------------------------------------------------------------------------- persistence


def test_node_vectors_round_trip_on_the_graph_doc():
    doc = ActionGraphDoc(
        _id="graph-1",
        user_id="user-1",
        name="g",
        node_vectors={"n1": baseline_vector(an_action_doc(), start_node_id=START)},
    )

    reloaded = ActionGraphDoc.model_validate(doc.model_dump(by_alias=True))

    assert reloaded.node_vectors["n1"].baseline.entries[0].node_id == START


def test_a_graph_stored_before_this_change_still_validates():
    doc = ActionGraphDoc(_id="graph-1", user_id="user-1", name="g")

    assert doc.node_vectors is None


def test_update_defaults_to_none_so_a_save_cannot_wipe_run_state():
    assert ActionGraphUpdate(name="renamed").node_vectors is None


def test_a_run_freezes_node_vectors_with_the_rest_of_the_graph():
    from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
        ActionGraphResultDoc,
    )
    from src.autobots.event_result.event_result_model import EventResultStatus, EventType

    graph = ActionGraphDoc(
        _id="graph-1",
        user_id="user-1",
        name="g",
        node_vectors={"n1": baseline_vector(an_action_doc(), start_node_id=START)},
    )
    run = ActionGraphResultDoc(
        _id="run-1",
        user_id="user-1",
        type=EventType.action_graph_run,
        status=EventResultStatus.processing,
        result=graph,
    )

    frozen = ActionGraphResultDoc.model_validate(run.model_dump(by_alias=True))

    assert frozen.result.node_vectors["n1"].baseline.entries[0].turn_no == 0
