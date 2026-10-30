"""Turn numbers, handed out per `(node, context_index)` - the ticket machine (§Part 3.5, §2.1).

A turn number used to be derived as "how many turns this tag has already **recorded**":

    len(vector.slot(context_index).tagged(node))

A turn that has been *opened and queued* has written nothing yet, so the count cannot see it. With
one redirect edge that is harmless - open a turn, run it, it writes, the next read sees the new
count. With two edges leaving one node several turns are in flight at once and the count only knows
about the ones that finished, so two turns take the same `(turn_no, tag)`. They then land in the
same cell, `upsert_entry` replaces on that identity, and the later turn erases the earlier one -
measured on `s -> a -> b -> z` with `z ⇢ a` and `z ⇢ b`: **six turns fired, four recorded**.

A ticket machine hands out 1, 2, 3 and remembers what it gave. Counting how many people have been
*served* gives the same number twice whenever two are being served at once.

### "turn" and "lap" are two different things - do not use them interchangeably

| | | |
|---|---|---|
| **turn** | one trip round one loop, and the number identifying it | counted here, saved on the document |
| **lap**  | one wave of the work queue - which round of scheduling | `frontier.ScheduledNode.lap`, in memory only, never saved |

They are not in step: **one lap can carry several turns**, because a node with two redirect edges
opens two turns in one firing and both are queued into the same next lap. The plan's prose uses
"lap" for both, which is where the confusion starts; in this code "lap" means the scheduler wave
and nothing else.

**The counters live on the run document, never in memory**, for the same reason as
`redirect_budget` (RISK-4): three retry layers stack - Temporal ×3, tenacity ×3, attachment ×3 -
and an in-memory counter resets under each, restarting the numbering and overwriting the transcript
it already wrote. On the document, a retry *and* a resume both continue the numbering.

**`None`, never `{}`.** A `{}` default would be written on every partial save and make a DAG's run
document differ from one produced before cyclic support existed - the same discipline
`node_vectors` and `redirect_budgets` follow. The map is created on the first firing and a DAG
never fires, so it stays absent.

Deliberately *not* derived from `redirect_budgets`. The arithmetic would work - the turn is the sum
of `fired` over the edges leaving the node - but the budget is a guardrail a human edits to resume
an exhausted run (D5), and §1.12 empties a vector's counters without touching budgets (OQ-6).
Turn numbering must not move when either of those happens.

This module is pure data and pure functions, so `action_graph_doc_model` can import it the way it
already imports `context_vector` and `redirect_budget`.
"""

from __future__ import annotations

from typing import Dict, Iterable, Mapping, Optional, Tuple

# Same separator as `redirect_budget`: node ids are digit strings or uuid-like, so a pipe cannot
# collide with one, and it is safe as a Mongo key unlike `.` and a leading `$`.
KEY_SEPARATOR = "|"


def turn_key(node: str, context_index: int | str) -> str:
    """The stable key for one column: the redirecting node and the slot it owns.

    Keyed by `(node, slot)` and **not** by edge, because §1.3 indexes the context vector by the
    redirecting node: every edge leaving `node` writes the same column, so all of them draw from
    one counter. Numbering per edge would hand out `0` twice and reintroduce the collision.
    """
    return f"{node}{KEY_SEPARATOR}{context_index}"


def split_key(key: str) -> Tuple[str, str]:
    """`(node, context_index)` back out of a key. Inverse of `turn_key`."""
    node, _, context_index = key.partition(KEY_SEPARATOR)
    return node, context_index


def turns_taken(
    counters: Optional[Mapping[str, int]],
    node: str,
    context_index: int | str,
    *,
    seed: int = 0,
) -> int:
    """How many turns this column has handed out - the turn the next one will take.

    `seed` is consulted **only when the column has no counter yet**, which is what carries a run
    that started before this field existed: its transcript is the only record of how many turns it
    opened, so the count of recorded entries seeds the machine rather than authoring it. Once the
    key exists the stored value wins, because it is the one that also knows about turns still in
    flight.
    """
    return (counters or {}).get(turn_key(node, context_index), seed)


def next_turn(
    counters: Optional[Mapping[str, int]],
    node: str,
    context_index: int | str,
    *,
    seed: int = 0,
) -> Tuple[Dict[str, int], int]:
    """Hand out the next turn for this column, and remember it was handed out.

    Returns `(updated counters, turn_no)`. One call, because handing out a number and recording
    that it was handed out are the same event - splitting them into `peek` then `bump` is what
    allows a caller to read twice and issue one number to two turns.

    Returns a new map rather than mutating, so a caller cannot advance a counter it only meant to
    inspect (`turns_taken` is the read).
    """
    updated: Dict[str, int] = dict(counters or {})
    key: str = turn_key(node, context_index)
    # `seed` is a FLOOR, not a default. As a default it was only consulted when the column had no
    # counter, which left the counter blind to numbers taken by anything other than this column -
    # a user operation (a continue-run, a chat with one node) writes into every slot and can leave
    # an entry numbered above what this counter has reached. The next redirect then reuses that
    # number, and the two entries sort as equal.
    #
    # Raising only is what keeps Part 3.5's fix intact: a turn already handed out but still in
    # flight has written nothing, so a transcript-derived floor is *lower* than the counter, and
    # `max` keeps the counter. The floor can never pull numbering backwards over an open turn.
    turn_no = max(updated.get(key, seed), seed)
    updated[key] = turn_no + 1
    return updated, turn_no


def open_turn_key(node: str, context_index: int | str, turn_no: int) -> str:
    """The stable key for one turn that has been handed out: who opened it, where, and which.

    Keyed by the redirecting node and column like `turn_key`, plus the turn itself, because
    several turns of one column can be outstanding at once.
    """
    return KEY_SEPARATOR.join((node, str(context_index), str(turn_no)))


def split_open_turn_key(key: str) -> Tuple[str, str, int]:
    """`(node, context_index, turn_no)` back out of a key. Inverse of `open_turn_key`."""
    node, context_index, turn_no = key.split(KEY_SEPARATOR)
    return node, context_index, int(turn_no)


def continue_run_key(root: str, turn_no: int) -> str:
    """The stable key for a continue-run that has been started: which node, and which turn.

    A continue-run is a turn like any other - one wave of work under one `(tag, turn_no)` - but
    its frame lived only in local variables, so a human gate mid-operation did not pause it, it
    **cancelled** it. The next run had no idea an operation was half done, asked every node about
    turn 0, found them all done, and reported success having executed nothing.

    The root doubles as the tag, so these two fields are the whole frame: the participants are
    recomputable from the root (its subtree, minus itself) and who still owes the turn is a
    lookup. Same reasoning as `open_turn_key`, which stores the redirect target because that is
    the one piece `R.S.(i, j)` cannot be recovered without.
    """
    return f"{root}{KEY_SEPARATOR}{turn_no}"


def split_continue_run_key(key: str) -> Tuple[str, int]:
    """`(root, turn_no)` back out of a key. Inverse of `continue_run_key`."""
    root, _, turn_no = key.partition(KEY_SEPARATOR)
    return root, int(turn_no)


def record_open_turn(
    open_turns: Optional[Mapping[str, str]],
    node: str,
    context_index: int | str,
    turn_no: int,
    *,
    target: str,
) -> Dict[str, str]:
    """Remember that this turn was handed out, and **which edge opened it** (RISK-12).

    The turn counter is already durable; the *open frames* were not, so nothing recorded that a
    turn had been handed out and never completed. On a retry the frontier is rebuilt empty, every
    node reads as done for the forward pass, and the redirecting node fires again taking the
    **next** number - leaving the nodes that never recorded the interrupted turn never recording
    it, and the abandoned turn still charged to the budget.

    The value is the redirect **target**, which is the one thing that was missing: `R.S.(i, j)` is
    looked up as `(target, node)`, so without it there is no way to know which nodes owed the turn.
    Everything else needed to rebuild the frame is already in the key.

    Returns a new map rather than mutating, matching `next_turn` and `record_firing`.
    """
    updated = dict(open_turns or {})
    updated[open_turn_key(node, context_index, turn_no)] = target
    return updated


def close_open_turns(
    open_turns: Optional[Mapping[str, str]], completed: Iterable[str]
) -> Optional[Dict[str, str]]:
    """Drop the turns that have finished, keeping the map to what is genuinely outstanding.

    Returns None when nothing is left and nothing was there to begin with, so a DAG's document
    never gains the key - the same discipline `node_vectors` and `redirect_budgets` follow.
    """
    if not open_turns:
        return None
    done = set(completed)
    remaining = {k: v for k, v in open_turns.items() if k not in done}
    # Back to None rather than {} when the last one closes: an empty map would be written on every
    # later save and make the document differ from one whose run never opened a turn.
    return remaining or None


__all__ = [
    "KEY_SEPARATOR",
    "close_open_turns",
    "continue_run_key",
    "next_turn",
    "open_turn_key",
    "record_open_turn",
    "split_continue_run_key",
    "split_key",
    "split_open_turn_key",
    "turn_key",
    "turns_taken",
]
