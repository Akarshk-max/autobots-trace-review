"""The redirect stack (§1.8) - open redirections, not traversal.

Three rules carry the whole scheduler and each is tested against its wrong version:

  1. The pop condition is **two-part**. Matching only the node unwinds turns that are still open.
  2. Handing a stack to N children must not let one child's push reach the others (§1.8.1).
  3. All queues arriving at a fan-in are equal, so take any one and **assert** the rest (C1/C3) -
     never merge them.
"""

from __future__ import annotations

import pytest

from src.autobots.action_graph.action_graph.context_vector import BASELINE_SLOT
from src.autobots.action_graph.action_graph.redirect_stack import (
    EMPTY,
    RedirectFrame,
    agree,
    stacks_from,
)
from src.autobots.data_model.context import Context

# --------------------------------------------------------------------------- reading


def test_a_fresh_stack_reads_as_the_forward_pass():
    assert EMPTY.is_empty
    assert EMPTY.top is None
    assert EMPTY.context_index == BASELINE_SLOT, "no open redirect means the baseline"
    assert EMPTY.depth == 0


def test_the_top_is_the_nearest_enclosing_redirection():
    """Q = [q(n-1), ..., q0]; the top is what `context_index` must report (C2)."""
    stack = EMPTY.push("2", "1", target="0").push("5", "2", target="0")

    assert stack.top == RedirectFrame(node="5", context_index="2", target="0")
    assert stack.context_index == "2"
    assert stack.open_nodes == ("2", "5"), "outermost first"
    assert stack.depth == 2


def test_a_frame_cannot_claim_the_baseline_slot():
    """Slot 0 is the context of *no* redirect.

    A frame claiming it would make `closes` match on the forward pass and unwind a turn that never
    opened.
    """
    with pytest.raises(ValueError, match="baseline"):
        RedirectFrame(node="2", context_index=BASELINE_SLOT, target="0")


def test_an_int_context_index_is_normalised_to_the_slot_key():
    """`GraphTopology.context_index` hands out ints; vector slots are keyed by str."""
    assert EMPTY.push("2", 1, target="0").context_index == "1"


# --------------------------------------------------------------------------- the pop condition


def test_reaching_the_redirecting_node_in_its_own_context_closes_the_turn():
    stack = EMPTY.push("2", "1", target="0")

    assert stack.closes("2", "1")
    assert stack.close_if_complete("2", "1").is_empty


def test_reaching_the_same_node_under_a_different_context_does_not_close_it():
    """The half of the condition that is easy to drop.

    'The same node arrived at under a different context is a different event and must not pop the
    entry.' Under a node-only check this pops, and a turn that is still open is unwound.
    """
    stack = EMPTY.push("2", "1", target="0")

    assert not stack.closes("2", "2"), "different context - not this turn"
    assert stack.close_if_complete("2", "2") == stack, "unchanged"


def test_reaching_a_different_node_does_not_close_the_turn():
    stack = EMPTY.push("2", "1", target="0")

    assert not stack.closes("5", "1")
    assert stack.close_if_complete("5", "1") == stack


def test_only_the_innermost_turn_closes():
    """A nested redirect must close before the one it is nested inside (Lemma 2)."""
    stack = EMPTY.push("2", "1", target="0").push("5", "2", target="0")

    assert not stack.closes("2", "1"), "the outer turn is not on top"
    assert stack.closes("5", "2")

    inner_closed = stack.close_if_complete("5", "2")
    assert inner_closed.open_nodes == ("2",)
    assert inner_closed.closes("2", "1"), "now the outer one is reachable"


def test_an_empty_stack_closes_nothing():
    assert not EMPTY.closes("2", "1")
    assert EMPTY.close_if_complete("2", "1") is EMPTY
    with pytest.raises(IndexError):
        EMPTY.pop()


def test_the_same_node_can_be_open_at_two_depths():
    """'The next redirect pushes again at the new depth.'"""
    stack = EMPTY.push("2", "1", target="0").push("2", "1", target="0")

    assert stack.depth == 2
    assert stack.close_if_complete("2", "1").depth == 1, "one turn at a time"


# --------------------------------------------------------------------------- §1.8.1 propagation


def test_a_push_on_one_branch_is_invisible_to_a_sibling():
    """The invariant §1.8.2 rests on, achieved by immutability rather than by copying.

    Under a mutable stack shared by reference this fails: the sibling sees a frame it never pushed,
    and a later mutation can retroactively change a queue a node already received.
    """
    handed_to_children = EMPTY.push("2", "1", target="0")

    left = handed_to_children.push("5", "2", target="0")
    right = handed_to_children

    assert left.open_nodes == ("2", "5")
    assert right.open_nodes == ("2",), "the sibling's queue was mutated"
    assert handed_to_children.open_nodes == ("2",), "the parent's queue was mutated"


def test_a_pop_on_one_branch_is_invisible_to_a_sibling():
    handed_to_children = EMPTY.push("2", "1", target="0").push("5", "2", target="0")

    left = handed_to_children.pop()

    assert left.open_nodes == ("2",)
    assert handed_to_children.open_nodes == ("2", "5")


# --------------------------------------------------------------------------- the edge, not the node


def test_a_frame_names_the_edge_that_opened_it():
    """A turn belongs to an edge. The node alone cannot say which one."""
    stack = EMPTY.push("2", "1", target="0")

    assert stack.top.node == "2", "who fired"
    assert stack.top.target == "0", "where it was aimed"
    assert stack.top.context_index == "1", "whose column the turn writes"


def test_two_edges_from_one_node_share_a_column_but_not_a_frame():
    """§1.3: the vector is indexed by the redirecting node, so both edges write slot 1.

    That is exactly why the target has to live on the frame - it is the only thing left that tells
    the two turns apart, and `R.S.(i, j)` is keyed by it.
    """
    to_a = EMPTY.push("z", "1", target="a", turn_no=0)
    to_b = EMPTY.push("z", "1", target="b", turn_no=1)

    assert to_a.context_index == to_b.context_index == "1", "one slot per R.N., not per edge"
    assert to_a != to_b, "but two distinct turns"
    assert {to_a.top.target, to_b.top.target} == {"a", "b"}


def test_the_target_has_no_default():
    """A frame carrying `""` would miss the `R.S.(i, j)` lookup and pass every check built on it."""
    with pytest.raises(TypeError):
        RedirectFrame(node="2", context_index="1")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        EMPTY.push("2", "1")  # type: ignore[call-arg]


def test_stacks_are_comparable_by_value():
    """C3's assert is an equality check, so equal-by-content stacks must compare equal."""
    assert EMPTY.push("2", "1", target="0") == EMPTY.push("2", "1", target="0")
    assert EMPTY.push("2", "1", target="0") != EMPTY.push("2", "2", target="0")
    assert EMPTY.push("2", "1", target="0") != EMPTY.push("5", "1", target="0")
    assert EMPTY.push("2", "1", target="0").push("5", "2", target="0") != EMPTY.push(
        "5", "2", target="0"
    ).push("2", "1", target="0")
    assert EMPTY.push("z", "1", target="a") != EMPTY.push("z", "1", target="b"), (
        "same node, same column, different edge"
    )


# --------------------------------------------------------------------------- C1 / C3 at a fan-in


def test_agreeing_queues_return_the_one_to_use():
    stack = EMPTY.push("2", "1", target="0")

    assert agree(Context(), [stack, stack, stack], node="a") == stack


def test_no_parents_means_the_forward_pass():
    assert agree(Context(), [], node="a") is EMPTY


def test_disagreeing_queues_are_reported_and_do_not_take_the_run_down():
    """R3-4: a canary that fails production runs is a worse canary.

    The default is log-and-continue with the first queue - never a merge. A mismatch means Lemma 1
    or Lemma 2 was violated, which is a bug to raise, not a state to reconcile.
    """
    left = EMPTY.push("2", "1", target="0")
    right = EMPTY

    assert agree(Context(), [left, right], node="a") == left


def test_disagreeing_queues_raise_under_strict():
    left = EMPTY.push("2", "1", target="0")
    right = EMPTY

    with pytest.raises(AssertionError, match="disagree"):
        agree(Context(), [left, right], node="a", strict=True)


def test_a_parent_that_sent_nothing_is_not_an_empty_queue():
    """An incomparable parent is not in R.S., so it does not re-run and delivers *nothing*.

    Treating that absence as an empty stack would make an unrelated branch look like it had voted
    for the forward pass, and the C3 assert would fire on a graph that is behaving correctly.
    """
    inside_a_turn = EMPTY.push("2", "1", target="0")

    delivered = stacks_from([inside_a_turn, None, inside_a_turn])

    assert delivered == [inside_a_turn, inside_a_turn]
    assert agree(Context(), delivered, node="a", strict=True) == inside_a_turn


def test_conflating_a_missing_parent_with_an_empty_queue_would_trip_the_canary():
    """The wrong version of the test above, made explicit."""
    inside_a_turn = EMPTY.push("2", "1", target="0")

    with pytest.raises(AssertionError):
        agree(Context(), [inside_a_turn, EMPTY], node="a", strict=True)


# --------------------------------------------------------------------------- multiple stacks


def test_two_redirect_chains_carry_independent_stacks():
    """'Multiple stacks can coexist. Each redirect chain carries its own.'"""
    chain_a = EMPTY.push("2", "1", target="0")
    chain_b = EMPTY.push("5", "2", target="0")

    assert chain_a.context_index == "1"
    assert chain_b.context_index == "2"
    assert chain_a != chain_b
