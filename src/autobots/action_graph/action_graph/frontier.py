"""The frontier scheduler - a work queue that can put a node back (§Part 3, §2.1).

The layer walk it replaces computes `coffman_graham(...)` once and iterates it, so a node's
position is fixed before the run starts and nothing can re-enter it. That is the first of the three
reasons the engine cannot cycle.

**A DAG must execute in exactly the order it does today**, so the queue is *seeded in
Coffman-Graham order*: rank is the node's flattened position in the layering, and lap 0 is drained
in rank order. That gives one code path for both shapes rather than a cyclic scheduler beside a
DAG one, and the ordering is unchanged for every graph that has no redirect edges - the property
Parts 1 and 2 both held and this branch must not be the one to drop.

Ordering is `(lap, rank, sequence)`:

  - **lap** - wave semantics. A lap completes before the next begins, which is what "the wave passes
    through *b*" means in §1.8.2's Lemma 2.
  - **rank** - topological order within a lap, inherited from the layering.
  - **sequence** - insertion order, so equal keys stay stable rather than comparing nodes.

`rearm` is the whole point and nothing calls it for a DAG: with no redirect edges there is never a
second lap, so the queue drains once and the scheduler is observationally the old loop.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

from src.autobots.action_graph.action_graph.redirect_stack import EMPTY, RedirectStack


@dataclass(frozen=True)
class ScheduledNode:
    """One node, scheduled for one execution."""

    node: str
    lap: int
    layer: int
    stack: RedirectStack = EMPTY

    @property
    def context_index(self) -> str:
        """The slot this execution reads and writes - the nearest enclosing redirect, or baseline."""
        return self.stack.context_index

    @property
    def tag(self) -> Optional[str]:
        """Provenance: the redirecting node, or None on the forward pass (where it is the start)."""
        top = self.stack.top
        return top.node if top else None

    @property
    def turn_no(self) -> int:
        """The turn this execution records. The forward pass is turn 0 for the start tag."""
        top = self.stack.top
        return top.turn_no if top else 0

    @property
    def redirect_target(self) -> Optional[str]:
        """Where the redirect that opened this lap was aimed - None on the forward pass.

        With `tag` (the source) this names the edge, which is what `R.S.(i, j)` is keyed by. The
        slot cannot supply it: every edge leaving one node shares that node's column (§1.3).
        """
        top = self.stack.top
        return top.target if top else None


@dataclass(order=True)
class _QueueItem:
    lap: int
    rank: int
    sequence: int
    node: str = field(compare=False)
    layer: int = field(compare=False)
    stack: RedirectStack = field(compare=False, default=EMPTY)


class FrontierScheduler:
    """A re-armable work queue over a Coffman-Graham layering."""

    def __init__(self, layers: Sequence[Sequence[str]]):
        self._layer_of: dict[str, int] = {}
        self._rank: dict[str, int] = {}
        rank = 0
        for layer_index, layer in enumerate(layers or ()):
            for node in layer:
                # A node repeated across layers keeps its first position, so rank stays a function
                # of the node rather than of which copy was seen last.
                if node in self._rank:
                    continue
                self._rank[node] = rank
                self._layer_of[node] = layer_index
                rank += 1

        self._sequence = itertools.count()
        self._heap: List[_QueueItem] = []
        for node, node_rank in sorted(self._rank.items(), key=lambda kv: kv[1]):
            heapq.heappush(
                self._heap,
                _QueueItem(
                    lap=0,
                    rank=node_rank,
                    sequence=next(self._sequence),
                    node=node,
                    layer=self._layer_of[node],
                ),
            )

    # ------------------------------------------------------------------ reading

    def __len__(self) -> int:
        return len(self._heap)

    @property
    def has_work(self) -> bool:
        return bool(self._heap)

    def rank_of(self, node: str) -> int:
        """Unknown nodes sort last rather than raising - a redirect target added mid-run is a
        scheduling question, not a crash."""
        return self._rank.get(node, len(self._rank))

    # ------------------------------------------------------------------ draining

    def pop(self) -> Optional[ScheduledNode]:
        if not self._heap:
            return None
        item = heapq.heappop(self._heap)
        return ScheduledNode(node=item.node, lap=item.lap, layer=item.layer, stack=item.stack)

    def __iter__(self):
        while (scheduled := self.pop()) is not None:
            yield scheduled

    # ------------------------------------------------------------------ re-entry

    def rearm(
        self, nodes: Iterable[str], *, lap: int, stack: RedirectStack = EMPTY
    ) -> Tuple[str, ...]:
        """Queue `nodes` for `lap`, in topological order, each carrying `stack`.

        Returns what was queued, in the order it will run - the caller logs it, and a test can
        assert the re-run set without draining the queue.
        """
        queued = sorted(set(nodes or ()), key=self.rank_of)
        for node in queued:
            heapq.heappush(
                self._heap,
                _QueueItem(
                    lap=lap,
                    rank=self.rank_of(node),
                    sequence=next(self._sequence),
                    node=node,
                    layer=self._layer_of.get(node, 0),
                    stack=stack,
                ),
            )
        return tuple(queued)


__all__ = ["FrontierScheduler", "ScheduledNode"]
