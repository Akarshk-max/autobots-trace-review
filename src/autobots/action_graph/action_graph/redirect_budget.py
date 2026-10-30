"""Per-`(i, j)` redirect budgets - the guardrail that stops a cycle running forever.

One counter per redirect edge, so `|R.E.|` rows rather than N². The granularity is deliberate
(§1.9): a global counter trips on a wide graph with many independent healthy cycles, and a
per-node counter conflates distinct loops that happen to share a node. Per-pair matches how the
context vector is already indexed.

**Counters live on the run document, never in memory.** Three retry layers already stack - Temporal
×3, tenacity ×3, attachment ×3 - so an in-memory counter resets on retry and the budget silently
stops bounding anything (RISK-4). Storing them on the document means a retry resumes the count.

On exhaustion the run pauses and asks a human (D5): status `waiting`, the exhausted pairs readable
off the document, resume via the existing same-`result_id` path.

This module is pure data and pure functions. It does not import the run document, so
`action_graph_doc_model` can import it the way it already imports `context_vector`.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from pydantic import BaseModel, Field

# Node ids are digit strings or uuid-like, so a pipe cannot collide with one. It is also safe as a
# Mongo key, unlike `.` and a leading `$`.
KEY_SEPARATOR = "|"


class RedirectBudget(BaseModel):
    """How many times one redirect edge may fire, and how many times it has."""

    fired: int = Field(default=0, ge=0)
    limit: int = Field(default=3, ge=0, description="0 disables the edge entirely")

    @property
    def is_disabled(self) -> bool:
        """A limit of 0 means the edge may never fire. That is not the same as running out.

        `fired >= limit` is true from the start for a zero limit, so without this distinction a
        deliberately disabled edge reports as exhausted - and an exhausted pair parks the run at
        `waiting`, asking a human to raise a limit they set to zero on purpose.
        """
        return self.limit == 0

    @property
    def is_exhausted(self) -> bool:
        """Ran out of a budget it actually had. A disabled edge never had one."""
        return not self.is_disabled and self.fired >= self.limit

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.fired)


def budget_key(target: str, source: str) -> str:
    """The stable key for redirect edge `source ⇢ target`, i.e. the pair `(i, j)`.

    Target first because the plan writes the pair as `(i, j)` - `i` is the redirect's destination,
    `j` its source. Keeping that order means a key reads the same way the documents do.
    """
    return f"{target}{KEY_SEPARATOR}{source}"


def split_key(key: str) -> Tuple[str, str]:
    """`(target, source)` back out of a key. Inverse of `budget_key`."""
    target, _, source = key.partition(KEY_SEPARATOR)
    return target, source


def initial_budgets(
    redirect_edges: Iterable, *, default_limit: int
) -> Optional[Dict[str, RedirectBudget]]:
    """One zeroed counter per redirect edge, or `None` when there are none.

    `None` rather than `{}` is the whole point: a DAG must carry no budget field at all, so its run
    document stays byte-identical to one produced before cyclic support existed - the same
    discipline `node_vectors` follows.
    """
    budgets = {
        budget_key(edge.target, edge.source): RedirectBudget(limit=default_limit)
        for edge in redirect_edges or ()
    }
    return budgets or None


def record_firing(
    budgets: Optional[Mapping[str, RedirectBudget]],
    target: str,
    source: str,
    *,
    default_limit: int,
) -> Dict[str, RedirectBudget]:
    """Count one firing of `source ⇢ target`, returning the updated map.

    Returns a new map rather than mutating, so a caller cannot accidentally advance a counter it
    only meant to inspect. An edge absent from the map is created at the default limit, which keeps
    the counter honest on a graph whose edges changed between runs.
    """
    updated = dict(budgets or {})
    key = budget_key(target, source)
    current = updated.get(key) or RedirectBudget(limit=default_limit)
    updated[key] = current.model_copy(update={"fired": current.fired + 1})
    return updated


def may_fire(
    budgets: Optional[Mapping[str, RedirectBudget]], target: str, source: str
) -> bool:
    """May `source ⇢ target` fire at all?

    False for a disabled edge as well as an exhausted one - the two differ in what the caller
    should *do* about it (skip quietly vs park and ask), not in whether the edge fires. Callers
    that need to tell them apart ask `is_disabled` first; callers that only need the yes/no answer
    get the safe one from here.

    An edge with no counter yet has not fired, so it may. Absence is "not started", not "denied".
    """
    budget = (budgets or {}).get(budget_key(target, source))
    if budget is None:
        return True
    return not budget.is_disabled and not budget.is_exhausted


def is_disabled(
    budgets: Optional[Mapping[str, RedirectBudget]], target: str, source: str
) -> bool:
    """Is `source ⇢ target` switched off outright? An edge with no counter yet is not."""
    budget = (budgets or {}).get(budget_key(target, source))
    return bool(budget and budget.is_disabled)


def exhausted_pairs(
    budgets: Optional[Mapping[str, RedirectBudget]],
) -> List[Tuple[str, str]]:
    """Every `(target, source)` that has hit its limit, stably ordered.

    Derived rather than stored. Recording exhaustion separately would be a second source of truth
    that could disagree with the counters - the same reason `EdgeSpec` carries no `type` field.
    """
    return [split_key(key) for key, budget in sorted((budgets or {}).items()) if budget.is_exhausted]


__all__ = [
    "KEY_SEPARATOR",
    "RedirectBudget",
    "budget_key",
    "exhausted_pairs",
    "is_disabled",
    "initial_budgets",
    "may_fire",
    "record_firing",
    "split_key",
]
