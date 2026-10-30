"""The ticket machine: turn numbers handed out per `(node, context_index)`.

The property under test is the one the old derivation could not hold - **a number handed out is
never handed out again**, even while several turns are in flight and none of them has written
anything yet. Each test below is written against the wrong version: counting recorded entries,
splitting the read from the bump, or numbering per edge instead of per column.
"""

from __future__ import annotations

from src.autobots.action_graph.action_graph.turn_counter import (
    next_turn,
    split_key,
    turn_key,
    turns_taken,
)

# --------------------------------------------------------------------------- handing out numbers


def test_successive_turns_take_successive_numbers():
    counters, first = next_turn(None, "z", "1")
    counters, second = next_turn(counters, "z", "1")
    counters, third = next_turn(counters, "z", "1")

    assert [first, second, third] == [0, 1, 2], "dense from 0, exactly as one edge produces today"


def test_a_number_is_never_handed_out_twice_while_turns_are_in_flight():
    """The whole point. Nothing is recorded between these calls - a queued turn has written nothing.

    Under the derivation this replaces (`len(slot.tagged(node))`) every one of these reads returns
    the same value, because no turn has finished.
    """
    counters = None
    handed_out = []
    for _ in range(6):
        counters, turn_no = next_turn(counters, "z", "1")
        handed_out.append(turn_no)

    assert handed_out == [0, 1, 2, 3, 4, 5]
    assert len(set(handed_out)) == len(handed_out), f"a number was reused: {handed_out}"


def test_every_edge_leaving_a_node_draws_from_one_counter():
    """§1.3: the vector is indexed by the redirecting node, so both edges write column 1.

    Numbering per edge would give each of them its own `0` - two turns, one cell, one survivor.
    The caller therefore never passes the target here, and this test is what says so.
    """
    counters, to_a = next_turn(None, "z", "1")
    counters, to_b = next_turn(counters, "z", "1")

    assert to_a != to_b
    assert set(counters) == {"z|1"}, "one counter for the column, not one per edge"


def test_columns_are_counted_independently():
    """Turns count per `(slot, tag)`, so two redirecting nodes never share a sequence."""
    counters, _ = next_turn(None, "z", "1")
    counters, _ = next_turn(counters, "z", "1")
    counters, other = next_turn(counters, "y", "2")

    assert other == 0, "y's column has opened nothing yet"
    assert turns_taken(counters, "z", "1") == 2


# --------------------------------------------------------------------------- reading vs writing


def test_reading_the_counter_does_not_advance_it():
    """`turns_taken` is the inspection. Only `next_turn` issues a number."""
    counters, _ = next_turn(None, "z", "1")

    assert turns_taken(counters, "z", "1") == 1
    assert turns_taken(counters, "z", "1") == 1
    assert counters == {"z|1": 1}


def test_next_turn_does_not_mutate_what_it_was_given():
    """A caller that only meant to inspect cannot advance a counter by accident."""
    before = {"z|1": 2}

    after, turn_no = next_turn(before, "z", "1")

    assert before == {"z|1": 2}, "the input map was mutated"
    assert after == {"z|1": 3}
    assert turn_no == 2


def test_an_unopened_column_reads_as_zero():
    assert turns_taken(None, "z", "1") == 0
    assert turns_taken({}, "z", "1") == 0


# --------------------------------------------------------------------------- the resume seed


def test_the_seed_is_used_only_when_the_column_has_no_counter():
    """A run that started before this field existed has a transcript and no counter.

    Its recorded entries are then the only record of how many turns it opened, so they seed the
    machine. Restarting at 0 instead would overwrite the transcript it already wrote.
    """
    counters, turn_no = next_turn(None, "z", "1", seed=3)

    assert turn_no == 3, "continue the numbering the transcript implies"
    assert counters == {"z|1": 4}


def test_the_seed_is_a_floor_and_raises_a_stored_counter():
    """**Reverses an earlier rule, deliberately.** This used to assert the stored counter wins
    unconditionally - `next_turn({"z|1": 1}, seed=5)` returned 1.

    That held while `seed` was the per-column *recorded count*, which can only ever lag the
    counter, so a seed above it meant something was inconsistent and the counter was the better
    answer. The seed is now a run-wide floor: a user operation - a continue-run, or a chat with one
    node - writes into every slot and can leave an entry numbered above what this column has
    reached. Ignoring the floor let the next redirect reuse that number, and the two entries sorted
    as equal.

    Part 3.5's actual concern is untouched, and `test_a_turn_in_flight_never_pulls_numbering_back`
    below is the test for it: a turn already handed out has written nothing, so a
    transcript-derived floor is *lower* than the counter and `max` keeps the counter. The floor
    only ever raises.
    """
    counters = {"z|1": 1}

    updated, turn_no = next_turn(counters, "z", "1", seed=5)

    assert turn_no == 5, "the floor must lift the counter past a number already taken elsewhere"
    assert updated["z|1"] == 6


def test_a_turn_in_flight_never_pulls_numbering_back():
    """The half of Part 3.5's rule that still holds, and the reason the floor is `max` not `=`.

    Three turns handed out, only two recorded because the third is still queued. A floor derived
    from the transcript sees the two and offers 2 - a number already issued. `max` keeps the
    counter's 3, so the queued turn's number is never handed out twice.
    """
    counters = {"z|1": 3}

    _, turn_no = next_turn(counters, "z", "1", seed=2)

    assert turn_no == 3, "a lagging floor must not claw the number back onto one already issued"


# --------------------------------------------------------------------------- keys


def test_keys_round_trip_and_stay_mongo_safe():
    key = turn_key("node-7", 2)

    assert key == "node-7|2"
    assert split_key(key) == ("node-7", "2")
    assert "." not in key and not key.startswith("$")


def test_an_int_slot_and_its_string_key_are_the_same_column():
    """`GraphTopology.context_index` hands out ints; vector slots are keyed by str."""
    counters, _ = next_turn(None, "z", 1)

    assert turns_taken(counters, "z", "1") == 1
