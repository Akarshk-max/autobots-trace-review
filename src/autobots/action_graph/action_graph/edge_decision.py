"""Per-`(edge, turn)` verdicts - the first state in the engine keyed by edge rather than by node.

**OQ-9, settled here.** One node runs once but can answer differently for each edge leaving it
(D9), so a verdict cannot live on the node's execution. It also cannot live on the turn alone: two
edges leaving one node share that node's column (§1.3), so the slot does not distinguish them
either. Edge **and** turn, therefore.

### What this is not

It is **not a readiness question**, and reading it as one is the trap the plan warns about. A
parent that declines an edge still ran and still produced output, so `is_work_done` passes and
nothing blocks. This engine cannot deadlock on a declined edge - §1.11's framing assumes a node
waits for a *message*, and this one checks whether *output exists*.

The requirement is **input composition**: `to_input` must leave a declined parent out of the input
it would otherwise contribute to. That is the whole job.

### How it is read

Not by `to_input` itself. The store answers one question - `declined_parents` - and the run paths
subtract that set from `upstream_nodes` before building the prompt. `to_input` keeps its signature
and its ignorance of turns.

**Absence means the parent contributes.** Nothing writes a verdict yet (Part 5 does), so every
graph running today has `edge_decisions is None`, gets an unfiltered parent list, and produces a
byte-identical prompt. Absence also covers a parent outside `R.S.` that did not re-run this turn:
it is not declining, it is simply contributing the value it already has (case (ii)).

### Where it does not live

Not on `ContextEntry`, which is the tempting home since the entry *is* the turn's record.
`upsert_entry` replaces the whole cell on every write, and a loop node writes progressively - once
per iteration - so a verdict stored there would be erased by the node's own second iteration.

### The redirect message lives here too

SS2.4 asks that a redirect's per-target text be attached to an `(edge, turn)` and read by the
target when it runs that turn. That is precisely this map's key, so `message` is a field on
`EdgeDecision` rather than a store of its own - no second map, no second null key on every DAG
document, and the two can never disagree about which turn they describe.

It is emphatically **not** `reason`, which is the decider accounting for its own choice (RISK-9)
and is never shown to anyone. `reason` explains the routing; `message` instructs the target.

Storage only in this branch. Part 4 wires the subtraction and the handback read; Part 5 writes
both the verdicts and the messages.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from pydantic import BaseModel, Field

# Same separator as `redirect_budget` and `turn_counter`, for the same reasons: node ids cannot
# contain it, and it is safe as a Mongo key unlike `.` and a leading `$`.
KEY_SEPARATOR = "|"


class EdgeDecision(BaseModel):
    """What one edge answered, on one turn - and, for a redirect, what it said."""

    fired: bool = Field(description="did this edge carry anything on this turn")
    reason: Optional[str] = Field(
        default=None,
        description=(
            "The decider's own words, kept so a surprising path is explainable afterwards "
            "(RISK-9). Never used to make a decision - only to account for one."
        ),
    )
    message: Optional[str] = Field(
        default=None,
        description=(
            "The redirect's handback: what the redirector is telling its target to do this turn "
            "('go back and fix the tone'). SS1.4's `k_input`, and the one thing a target reads "
            "that is not its own history. Distinct from `reason`, which is the decider accounting "
            "for its own choice and is never shown to the target. Written by Part 5."
        ),
    )


def decision_key(
    target: str, source: str, context_index: int | str, turn_no: int
) -> str:
    """The stable key for edge `source -> target` on the turn `(context_index, turn_no)`.

    Target first, matching `budget_key`, so a key reads the way the plan writes the pair `(i, j)`.
    Then the turn. `turn_no` alone would be ambiguous - turns count per `(slot, tag)`, so two
    columns both hold a turn 0 - and `context_index` alone cannot separate turns within a column.
    The tag is recoverable from the slot, so these two are sufficient and minimal.
    """
    return KEY_SEPARATOR.join((target, source, str(context_index), str(turn_no)))


def split_key(key: str) -> Tuple[str, str, str, int]:
    """`(target, source, context_index, turn_no)` back out of a key. Inverse of `decision_key`."""
    target, source, context_index, turn_no = key.split(KEY_SEPARATOR)
    return target, source, context_index, int(turn_no)


def record_decision(
    decisions: Optional[Mapping[str, EdgeDecision]],
    target: str,
    source: str,
    *,
    context_index: int | str,
    turn_no: int,
    fired: bool,
    reason: Optional[str] = None,
    message: Optional[str] = None,
) -> Dict[str, EdgeDecision]:
    """Record what one edge answered on one turn, and what it said, returning the updated map.

    Returns a new map rather than mutating, matching `record_firing`: a caller cannot record a
    verdict it only meant to inspect.

    `reason` and `message` are different things and must not be conflated: `reason` is the decider
    accounting for its own choice and is never shown to anyone; `message` is the instruction the
    target reads.
    """
    updated = dict(decisions or {})
    updated[decision_key(target, source, context_index, turn_no)] = EdgeDecision(
        fired=fired, reason=reason, message=message
    )
    return updated


def decision_for(
    decisions: Optional[Mapping[str, EdgeDecision]],
    target: str,
    source: str,
    *,
    context_index: int | str,
    turn_no: int,
) -> Optional[EdgeDecision]:
    """What this edge answered on this turn, or None if it was never asked."""
    return (decisions or {}).get(decision_key(target, source, context_index, turn_no))


def message_for(
    decisions: Optional[Mapping[str, EdgeDecision]],
    target: str,
    source: str,
    *,
    context_index: int | str,
    turn_no: int,
) -> Optional[str]:
    """What this redirect handed its target on this turn, or None if it said nothing.

    SS2.4 asks that a redirect message be attached to an `(edge, turn)` and read by the target when
    it runs that turn - which is exactly this map's key, so it needs no store of its own.

    Read by `context_input._target_input`, never by `to_input`. Nothing writes one until Part 5, so
    today every target reads its own transcript and nothing else.
    """
    decision = decision_for(
        decisions, target, source, context_index=context_index, turn_no=turn_no
    )
    return decision.message if decision else None


def has_declined(
    decisions: Optional[Mapping[str, EdgeDecision]],
    target: str,
    source: str,
    *,
    context_index: int | str,
    turn_no: int,
) -> bool:
    """Did this edge decline to carry anything on this turn?

    An edge never asked has **not** declined. Absence is "no verdict", not "no". Reading it the
    other way would drop every parent on every graph running today, since nothing writes a verdict
    until Part 5.
    """
    decision = decision_for(
        decisions, target, source, context_index=context_index, turn_no=turn_no
    )
    return decision is not None and not decision.fired


def declined_parents(
    decisions: Optional[Mapping[str, EdgeDecision]],
    node: str,
    upstream_nodes: Iterable[str],
    *,
    context_index: int | str,
    turn_no: int,
) -> List[str]:
    """Which of `node`'s parents declined to feed it on this turn, stably ordered.

    The one question the store exists to answer. The caller subtracts this from `upstream_nodes`
    before composing input, which is where a declined parent is meant to disappear.

    The parent and the child of an edge always run in the **same** turn - `rearm` hands one stack to
    every node in `R.S.` and the frame fixes the turn - so the child can look this up with its own
    run context and nothing has to be threaded from the parent's execution.
    """
    if not decisions:
        return []
    return [
        parent
        for parent in upstream_nodes or ()
        if has_declined(
            decisions, node, parent, context_index=context_index, turn_no=turn_no
        )
    ]


def contributing_parents(
    decisions: Optional[Mapping[str, EdgeDecision]],
    node: str,
    upstream_nodes: Iterable[str],
    *,
    context_index: int | str,
    turn_no: int,
) -> List[str]:
    """`upstream_nodes` minus the parents that declined, in the order given.

    Order is preserved because it is the order upstream blocks appear in the composed prompt.
    """
    declined = set(
        declined_parents(
            decisions, node, upstream_nodes, context_index=context_index, turn_no=turn_no
        )
    )
    return [parent for parent in upstream_nodes or () if parent not in declined]


__all__ = [
    "KEY_SEPARATOR",
    "EdgeDecision",
    "contributing_parents",
    "decision_for",
    "decision_key",
    "declined_parents",
    "has_declined",
    "message_for",
    "record_decision",
    "split_key",
]
