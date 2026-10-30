"""The redirect stack - which redirections are currently *open* (§1.8).

It does **not** track traversal. It tracks open redirections, and the top is always the nearest
enclosing one, which is why `top` is the correct context index to read and write.

```
Q = [ q(n-1) , ... , q1 , q0 ]        i > j  <=>  qi is an ancestor of qj
```

Push on every redirect. Pop **only** when execution reaches the redirecting node again *while still
inside the context that node's frame defines* - one full turn. Reaching the node is not sufficient:
the same node arrived at under a different context is a different event.

**Immutable by construction, which is how §1.8.1 is satisfied.** The plan requires that a queue be
handed to children by deep copy, never by reference, because under shared references a push on one
branch becomes visible to every sibling holding the same object, and a later sibling mutation can
retroactively change a queue a node has already received. Every operation here returns a *new*
stack and never mutates the receiver, so handing the same object to N children is safe: there is no
mutation for a sibling to observe. That gives the invariant §1.8.2 rests on with no copying at all,
which also sidesteps the `O(fan-out x depth)` cost the plan flagged and the copy-on-write
optimisation it deferred.

Multiple stacks coexist naturally: a stack is a value carried along a dispatch, never a singleton.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from loguru import logger

from src.autobots.action_graph.action_graph.context_vector import BASELINE_SLOT
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context


@dataclass(frozen=True)
class RedirectFrame:
    """One open redirection: the edge that fired it, and the context its turn runs under."""

    node: str
    context_index: str
    # Which edge opened this turn. The node alone does not say: a redirecting node may have several
    # outgoing redirect edges, and under Part 5 that is the normal shape. They all share the node's
    # column - the context vector is indexed by the redirecting node (§1.3), not by the edge - so
    # `context_index` cannot distinguish them either.
    #
    # Required, with no default, on purpose. `R.S.(i, j)` is looked up as `(target, node)`, and a
    # frame carrying `""` would miss that lookup, return the empty set, and turn every check built
    # on it into one that silently passes.
    target: str
    # Which turn this turn is for the tag `node`, counted per (slot, tag) and carried on the frame
    # rather than derived when the node runs. Derived, it would have to be "the next turn", which
    # is fresh by construction - so a resumed run would re-execute work it had already recorded
    # instead of recognising it. On the frame it is a fact about the turn: j's m-th redirect opens
    # turn m for tag j, whoever asks and however often.
    turn_no: int = 0

    def __post_init__(self) -> None:
        if self.context_index == BASELINE_SLOT:
            # Slot 0 is the baseline - the context of *no* redirect. A frame claiming it would make
            # the pop condition match on the forward pass and unwind a turn that never opened.
            raise ValueError(
                f"Redirect frame for node {self.node!r} cannot claim the baseline slot "
                f"{BASELINE_SLOT!r}; a redirecting node always has a slot of its own."
            )


@dataclass(frozen=True)
class RedirectStack:
    """An immutable stack of open redirections. Every operation returns a new stack."""

    frames: Tuple[RedirectFrame, ...] = ()

    # ------------------------------------------------------------------ reading

    @property
    def is_empty(self) -> bool:
        return not self.frames

    @property
    def depth(self) -> int:
        return len(self.frames)

    @property
    def top(self) -> Optional[RedirectFrame]:
        """The nearest enclosing redirection, or None on the forward pass."""
        return self.frames[-1] if self.frames else None

    @property
    def context_index(self) -> str:
        """The slot to read and write under right now.

        Empty stack means the forward pass, whose context is the baseline. This is C2: whichever
        parent you ask, the answer is the same, which is what gives a fan-in node a well-defined
        context at all.
        """
        top = self.top
        return top.context_index if top else BASELINE_SLOT

    @property
    def open_nodes(self) -> Tuple[str, ...]:
        """Outermost first - the order the invariant is written in."""
        return tuple(frame.node for frame in self.frames)

    def is_open(self, node: str) -> bool:
        return any(frame.node == node for frame in self.frames)

    # ------------------------------------------------------------------ writing

    def push(
        self, node: str, context_index: int | str, *, target: str, turn_no: int = 0
    ) -> "RedirectStack":
        """Open the redirection `node ⇢ target`. Returns a new stack; this one is unchanged."""
        frame = RedirectFrame(
            node=node, context_index=str(context_index), target=target, turn_no=turn_no
        )
        return RedirectStack(frames=self.frames + (frame,))

    def pop(self) -> "RedirectStack":
        """Close the innermost redirection. Returns a new stack."""
        if not self.frames:
            raise IndexError("pop from an empty redirect stack")
        return RedirectStack(frames=self.frames[:-1])

    # ------------------------------------------------------------------ the pop condition

    def closes(self, node: str, context_index: int | str) -> bool:
        """Does executing `node` under `context_index` close the current turn?

        Both halves are load-bearing. Matching only the node would pop on any arrival at the
        redirecting node, including one reached under a different context - a different event
        entirely, which would unwind a turn that is still open.
        """
        top = self.top
        if top is None:
            return False
        return top.node == node and top.context_index == str(context_index)

    def close_if_complete(self, node: str, context_index: int | str) -> "RedirectStack":
        """`pop()` when the turn has closed, otherwise unchanged. The scheduler's call."""
        return self.pop() if self.closes(node, context_index) else self


EMPTY = RedirectStack()


def agree(
    ctx: Context,
    queues: Sequence[RedirectStack],
    *,
    node: str,
    strict: bool = False,
) -> RedirectStack:
    """C1 + C3: take any one incoming queue, having asserted the rest are equal.

    By §1.8.2 every queue arriving at a node during one execution is identical, so there is
    **no merge, no reconciliation and no tie-break** - unlike `f_comb`, which merges transcripts.
    Do not confuse the two.

    A mismatch is not something to paper over: it means Lemma 1 or Lemma 2 has been violated - a
    push leaked across a wave boundary, or a stale queue was retained. It is reported loudly and,
    by default, execution continues with the first queue: a canary that takes down production runs
    is a worse canary (R3-4). `strict` raises instead, which is what the tests use.

    An empty sequence is a root node with no parents; its context is the forward pass.
    """
    if not queues:
        return EMPTY

    first = queues[0]
    mismatched = [q for q in queues[1:] if q != first]
    if mismatched:
        logger.bind(
            ctx=ctx,
            code=AppCode.ACTION_GRAPH_RUN,
            node_id=node,
            expected=list(first.open_nodes),
            received=[list(q.open_nodes) for q in queues],
        ).error(
            "Redirect queues disagree at a fan-in - the wave boundary invariant (§1.8.2) is broken"
        )
        if strict:
            raise AssertionError(
                f"Redirect queues disagree at node {node!r}: "
                f"{[list(q.open_nodes) for q in queues]}"
            )
    return first


def stacks_from(queues: Iterable[Optional[RedirectStack]]) -> List[RedirectStack]:
    """Drop the parents that sent nothing.

    A parent incomparable to the redirecting node is not in `R.S.` (D1), so it does not re-run this
    turn and delivers no queue at all - the node reads its stored value instead (case (ii)). That is
    an absence, not an empty stack, and conflating the two would make an unrelated branch look like
    it had voted for the forward pass.
    """
    return [q for q in queues if q is not None]


__all__ = [
    "EMPTY",
    "RedirectFrame",
    "RedirectStack",
    "agree",
    "stacks_from",
]
