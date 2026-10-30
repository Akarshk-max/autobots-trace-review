"""Structural facts about an action graph, resolved once.

`GraphTopology.build()` is the single place that reads the edge grammar. It answers who redirects,
which context slot each redirector owns, and which nodes a redirect affects - all of it derived up
front rather than rediscovered on every redirect at runtime.

Two rules shape this module:

- **`build()` never raises.** Validation ships in warn-only mode first, so a graph that fails
  `validate()` still has to run. Every traversal here is cycle-safe for that reason, and fields that
  cannot be determined (`start_node_id` on a malformed graph) are None rather than an exception.
- **The data-edge adjacency stays acyclic.** Cycles live only in `redirect_edges`, so `graph` keeps
  the DAG guarantees every existing traversal already depends on.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from pydantic import BaseModel

from src.autobots.action_graph.action_graph.action_graph_doc_model import (
    ActionGraphCreate,
    EdgeSpec,
)
from src.autobots.action_graph.action_graph.coffman_graham import (
    GraphCycleError,
    coffman_graham,
)

# Slot 0 is the baseline - the pre-redirection dimension, and the only one a user interacts with.
# It is stored separately from the redirector slots, which run 1..K-1.
BASELINE_CONTEXT_INDEX = 0


class ValidationCode(str, Enum):
    data_edge_cycle = "data_edge_cycle"
    no_start_node = "no_start_node"
    multiple_start_nodes = "multiple_start_nodes"
    start_node_id_mismatch = "start_node_id_mismatch"
    isolated_node = "isolated_node"
    unknown_edge_endpoint = "unknown_edge_endpoint"
    redirect_self_loop = "redirect_self_loop"
    redirect_target_not_an_ancestor = "redirect_target_not_an_ancestor"
    redirect_from_start_node = "redirect_from_start_node"
    duplicate_edge = "duplicate_edge"
    conditional_edge_not_in_graph = "conditional_edge_not_in_graph"
    conditional_edge_is_a_redirect = "conditional_edge_is_a_redirect"


class Severity(str, Enum):
    """How a validation issue should be treated.

    `warning` is for states a graph passes through while it is being built - a fresh canvas has no
    start node, a node dropped but not yet wired is isolated. Reporting those as errors would drown
    the warn-only failure-rate signal in ordinary authoring, and enforcement must never reject them.
    """

    error = "error"
    warning = "warning"


# Everything not listed here is an error: a real malformation, not a half-built graph.
_WARNING_CODES = frozenset(
    {
        ValidationCode.no_start_node,
        ValidationCode.isolated_node,
    }
)


class ValidationIssue(BaseModel):
    code: ValidationCode
    message: str
    nodes: List[str] = []
    severity: Severity = Severity.error

    def model_post_init(self, __context) -> None:
        if self.code in _WARNING_CODES:
            object.__setattr__(self, "severity", Severity.warning)


class EdgeCheck(BaseModel):
    """Whether an edge may be drawn, and why not if it may not.

    Truthy when allowed, so `if topo.check_redirect_edge(a, b):` reads naturally while `.reason`
    stays available for the message.
    """

    allowed: bool
    reason: Optional[ValidationIssue] = None

    def __bool__(self) -> bool:
        return self.allowed

    @classmethod
    def ok(cls) -> "EdgeCheck":
        return cls(allowed=True)

    @classmethod
    def no(cls, code: ValidationCode, message: str, nodes: List[str]) -> "EdgeCheck":
        return cls(
            allowed=False, reason=ValidationIssue(code=code, message=message, nodes=nodes)
        )


def _node_set(doc: ActionGraphCreate) -> Set[str]:
    """Every declared node, including ones with no edges."""
    nodes: Set[str] = set(doc.nodes or {})
    nodes |= set(doc.node_details or {})
    for source, targets in (doc.graph or {}).items():
        nodes.add(source)
        nodes.update(targets or [])
    return nodes


def _reachable(start: str, adjacency: Mapping[str, Sequence[str]]) -> FrozenSet[str]:
    """Inclusive reachable set. Cycle-safe: `build()` runs before anything is validated."""
    seen: Set[str] = {start}
    stack = [start]
    while stack:
        for neighbour in adjacency.get(stack.pop(), ()):
            if neighbour not in seen:
                seen.add(neighbour)
                stack.append(neighbour)
    return frozenset(seen)


def _edge_pairs(edges: Iterable[EdgeSpec]) -> List[Tuple[str, str]]:
    return [(e.source, e.target) for e in edges]


@dataclass(frozen=True)
class GraphTopology:
    nodes: FrozenSet[str]
    data_adj: Mapping[str, Tuple[str, ...]]  # node -> children, from `graph`
    data_rev: Mapping[str, Tuple[str, ...]]  # node -> parents
    redirect_edges: Tuple[EdgeSpec, ...]  # j -> i, meaning j redirects back into i
    # Kept as declared, so duplicates stay visible to validation; `conditional` below is the
    # de-duplicated lookup set the engine uses.
    conditional_edges: Tuple[EdgeSpec, ...]
    conditional: FrozenSet[Tuple[str, str]]

    # Every node with no data parent, stably ordered. Exactly one of these is the start node; 0 or
    # 2+ is what makes `start_node_id` None, and naming them is what makes that explainable.
    roots: Tuple[str, ...]

    # None when the graph has no single root; `validate()` reports why.
    start_node_id: Optional[str]

    redirecting_nodes: Tuple[str, ...]  # the R.N. set, stably ordered
    context_index: Mapping[str, int]  # R.N. -> slot 1..K-1; never contains start_node_id
    K: int  # |R.N.| + 1, the context vector width

    descendants: Mapping[str, FrozenSet[str]]  # subgraph(n), inclusive
    ancestors: Mapping[str, FrozenSet[str]]  # inclusive
    ancestor_rns: Mapping[str, FrozenSet[str]]
    descendant_rns: Mapping[str, FrozenSet[str]]
    relevant_subgraph: Mapping[Tuple[str, str], FrozenSet[str]]  # (i, j) -> R.S.(i, j)
    # (i, j) -> the slot keys a turn of that redirect writes. See `_propagation_slots`.
    propagation_slots: Mapping[Tuple[str, str], Tuple[str, ...]]

    _declared_start_node_id: Optional[str] = None

    @property
    def is_cyclic(self) -> bool:
        return self.K > 1

    @classmethod
    def build(cls, doc: ActionGraphCreate) -> "GraphTopology":
        """Accepts anything graph-shaped - ActionGraphDoc subclasses ActionGraphCreate,
        so a graph can be validated before it has been assigned an id."""
        nodes = _node_set(doc)

        data_adj: Dict[str, Tuple[str, ...]] = {n: () for n in nodes}
        data_rev: Dict[str, List[str]] = {n: [] for n in nodes}
        for source, targets in (doc.graph or {}).items():
            children = tuple(targets or ())
            data_adj[source] = children
            for child in children:
                data_rev[child].append(source)
        rev: Dict[str, Tuple[str, ...]] = {n: tuple(parents) for n, parents in data_rev.items()}

        descendants = {n: _reachable(n, data_adj) for n in nodes}
        ancestors = {n: _reachable(n, rev) for n in nodes}

        redirect_edges = tuple(doc.redirect_edges or ())
        conditional_edges = tuple(doc.conditional_edges or ())
        # R.N. are the redirect *sources*. The start node can never be one (it is the root, so a
        # descendant of nothing), which is what keeps the tag namespace {start} u R.N. disjoint.
        redirecting_nodes = tuple(sorted({e.source for e in redirect_edges}))
        context_index = {rn: idx for idx, rn in enumerate(redirecting_nodes, start=1)}

        rn_set = frozenset(redirecting_nodes)
        ancestor_rns = {n: ancestors[n] & rn_set for n in nodes}
        descendant_rns = {n: descendants[n] & rn_set for n in nodes}

        # Which slots a turn of `j` -> `i` writes: the context indices of the R.N.s that lie
        # INSIDE R.S.(i, j). Every node executing in that turn writes its own result into all of
        # them - a ROW update, one node across several of its own dimensions.
        #
        # A slot index *is* a redirecting node. If R.N. `r` lies inside the region this redirect
        # disturbs then what happens in the turn is part of what r's dimension should see, because
        # r is standing in it. An R.N. on an incomparable branch is genuinely untouched and its
        # slot stays put.
        #
        # NOT a column: no node ever writes into another node's slot. Doing that leaves a node
        # holding its *grandparent's* output, which it has no business seeing - it should know only
        # what its own parent handed it, which it gets by reading that parent's slot.
        #
        # `j` is always in R.S.(i, j) (it is a descendant of itself), so its own slot is always in
        # the set and this is never empty.
        #
        # R.S.(i, j) = (subgraph(i) & ancestors(j)) | descendants(j).
        # "Related to j" means comparable to j - an ancestor or a descendant. Nodes incomparable to
        # j sit on a branch that neither feeds it nor is fed by it, and take no part in the lap.
        # descendants(j) needs no clipping: j in subgraph(i) implies subgraph(j) subset subgraph(i).
        relevant_subgraph: Dict[Tuple[str, str], FrozenSet[str]] = {}
        for edge in redirect_edges:
            i, j = edge.target, edge.source
            if i in descendants and j in ancestors:
                relevant_subgraph[(i, j)] = (descendants[i] & ancestors[j]) | descendants[j]

        propagation_slots = {
            (i, j): tuple(
                sorted((str(context_index[r]) for r in (rn_set & rs)), key=int)
            )
            for (i, j), rs in relevant_subgraph.items()
        }

        roots = tuple(cls._roots(nodes, rev, data_adj))
        start_node_id = roots[0] if len(roots) == 1 else None

        return cls(
            nodes=frozenset(nodes),
            data_adj=data_adj,
            data_rev=rev,
            redirect_edges=redirect_edges,
            conditional_edges=conditional_edges,
            conditional=frozenset(_edge_pairs(conditional_edges)),
            roots=roots,
            start_node_id=start_node_id,
            redirecting_nodes=redirecting_nodes,
            context_index=context_index,
            K=len(redirecting_nodes) + 1,
            descendants=descendants,
            ancestors=ancestors,
            ancestor_rns=ancestor_rns,
            descendant_rns=descendant_rns,
            relevant_subgraph=relevant_subgraph,
            propagation_slots=propagation_slots,
            _declared_start_node_id=doc.start_node_id or None,
        )

    @staticmethod
    def _roots(
        nodes: Iterable[str],
        data_rev: Mapping[str, Sequence[str]],
        data_adj: Mapping[str, Sequence[str]],
    ) -> List[str]:
        """Nodes with no data parent. Isolated nodes are excluded - they have no parent either, so
        they would otherwise masquerade as extra roots and hide a genuine start node."""
        return sorted(
            n
            for n in nodes
            if not data_rev.get(n) and (data_adj.get(n) or data_rev.get(n))
        )

    def check_redirect_edge(
        self, source: str, target: str, *, as_new: bool = True
    ) -> EdgeCheck:
        """May `source` redirect back into `target`?

        `as_new=True` answers "can this edge be drawn", so an edge that already exists is refused
        as a duplicate. Pass `as_new=False` to check an edge that is already declared.
        """
        unknown = [n for n in (source, target) if n not in self.nodes]
        if unknown:
            return EdgeCheck.no(
                ValidationCode.unknown_edge_endpoint,
                f"Unknown node(s) {unknown}",
                unknown,
            )

        if source == target:
            return EdgeCheck.no(
                ValidationCode.redirect_self_loop,
                f"Redirect edge {source} -> {target} is a self-loop",
                [source],
            )

        # Called out before the descendant rule below, which it is a special case of, because
        # "the start node cannot redirect" is a far more intelligible message. A redirect *to*
        # the start node stays legal.
        if self.start_node_id and source == self.start_node_id:
            return EdgeCheck.no(
                ValidationCode.redirect_from_start_node,
                (
                    f"The start node {source!r} cannot redirect: a redirect source must be a "
                    "descendant of its target, and the start node is a descendant of nothing"
                ),
                [source],
            )

        if source not in self.descendants.get(target, frozenset()):
            return EdgeCheck.no(
                ValidationCode.redirect_target_not_an_ancestor,
                (
                    f"Redirect edge {source} -> {target} does not close a loop: {source} is not "
                    f"reachable from {target} along data edges"
                ),
                [source, target],
            )

        if as_new and (source, target) in set(_edge_pairs(self.redirect_edges)):
            return EdgeCheck.no(
                ValidationCode.duplicate_edge,
                f"Redirect edge {source} -> {target} already exists",
                [source, target],
            )

        return EdgeCheck.ok()

    def check_conditional_edge(
        self, source: str, target: str, *, as_new: bool = True
    ) -> EdgeCheck:
        """May the data edge `source -> target` be marked conditional?

        A conditional edge annotates an edge that already exists in `graph`; it does not create
        one. `as_new=True` refuses a pair that is already marked.
        """
        unknown = [n for n in (source, target) if n not in self.nodes]
        if unknown:
            return EdgeCheck.no(
                ValidationCode.unknown_edge_endpoint,
                f"Unknown node(s) {unknown}",
                unknown,
            )

        # A dotted edge pointing back at an ancestor is a redirect, not a conditional edge.
        # Checked before the not-in-graph rule below, which it would otherwise trip with a much
        # less useful message.
        if source != target and target in self.ancestors.get(source, frozenset()):
            return EdgeCheck.no(
                ValidationCode.conditional_edge_is_a_redirect,
                (
                    f"{target} is an ancestor of {source}, so {source} -> {target} is a redirect "
                    "edge, not a conditional one - declare it in redirect_edges"
                ),
                [source, target],
            )

        if target not in self.data_adj.get(source, ()):
            return EdgeCheck.no(
                ValidationCode.conditional_edge_not_in_graph,
                (
                    f"Conditional edge {source} -> {target} is not a data edge; a conditional "
                    "edge marks an edge that already exists in `graph`"
                ),
                [source, target],
            )

        if as_new and (source, target) in self.conditional:
            return EdgeCheck.no(
                ValidationCode.duplicate_edge,
                f"Conditional edge {source} -> {target} is already marked conditional",
                [source, target],
            )

        return EdgeCheck.ok()

    def isolated_nodes(self) -> List[str]:
        return sorted(n for n in self.nodes if not self.data_adj.get(n) and not self.data_rev.get(n))

    def validate(self) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        issues.extend(self._validate_structure())
        issues.extend(self._validate_start_node())
        issues.extend(self._validate_redirect_edges())
        issues.extend(self._validate_conditional_edges())
        return issues

    def _validate_structure(self) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []

        # Mirror what the engine actually does, so validation catches exactly what would fail at
        # runtime: run the layering over the inverted adjacency it uses.
        try:
            coffman_graham(dict(self.data_rev), len(self.nodes) or 1)
        except GraphCycleError as e:
            issues.append(
                ValidationIssue(
                    code=ValidationCode.data_edge_cycle,
                    message=(
                        "Data edges must be acyclic - express cycles as redirect_edges. "
                        f"Nodes unreachable by topological order: {sorted(e.unreachable_nodes)}"
                    ),
                    nodes=sorted(e.unreachable_nodes),
                )
            )

        isolated = self.isolated_nodes()
        if isolated:
            issues.append(
                ValidationIssue(
                    code=ValidationCode.isolated_node,
                    message=f"Nodes have no data edges and will never execute: {isolated}",
                    nodes=isolated,
                )
            )

        for label, edges in (
            ("redirect_edges", self.redirect_edges),
            ("conditional_edges", self.conditional_edges),
        ):
            for edge in edges:
                unknown = [n for n in (edge.source, edge.target) if n not in self.nodes]
                if unknown:
                    issues.append(
                        ValidationIssue(
                            code=ValidationCode.unknown_edge_endpoint,
                            message=f"{label} references unknown node(s) {unknown}",
                            nodes=unknown,
                        )
                    )

            pairs = _edge_pairs(edges)
            duplicates = sorted({p for p in pairs if pairs.count(p) > 1})
            for source, target in duplicates:
                issues.append(
                    ValidationIssue(
                        code=ValidationCode.duplicate_edge,
                        message=f"{label} contains {source} -> {target} more than once",
                        nodes=[source, target],
                    )
                )

        return issues

    def _validate_start_node(self) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        # Resolved once in `build`; recomputing here would let the two answers drift.
        roots = list(self.roots)

        if not roots:
            issues.append(
                ValidationIssue(
                    code=ValidationCode.no_start_node,
                    message="Graph has no start node: every node has an incoming data edge",
                )
            )
        elif len(roots) > 1:
            issues.append(
                ValidationIssue(
                    code=ValidationCode.multiple_start_nodes,
                    message=(
                        "Graph must have exactly one start node (a node with no incoming data "
                        f"edge); found {roots}"
                    ),
                    nodes=roots,
                )
            )

        declared = self._declared_start_node_id
        if declared and self.start_node_id and declared != self.start_node_id:
            issues.append(
                ValidationIssue(
                    code=ValidationCode.start_node_id_mismatch,
                    message=(
                        f"start_node_id is {declared!r} but the graph's only root is "
                        f"{self.start_node_id!r}"
                    ),
                    nodes=[declared, self.start_node_id],
                )
            )
        return issues

    def _validate_redirect_edges(self) -> List[ValidationIssue]:
        """Per-edge rules live in `check_redirect_edge`, so the "can I draw this?" answer and the
        "is this valid?" answer are the same code."""
        issues: List[ValidationIssue] = []
        for edge in self.redirect_edges:
            if edge.source not in self.nodes or edge.target not in self.nodes:
                continue  # already reported as an unknown endpoint
            check = self.check_redirect_edge(edge.source, edge.target, as_new=False)
            if check.reason:
                issues.append(check.reason)
        return issues

    def _validate_conditional_edges(self) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        for source, target in sorted(set(_edge_pairs(self.conditional_edges))):
            if source not in self.nodes or target not in self.nodes:
                continue  # already reported as an unknown endpoint
            check = self.check_conditional_edge(source, target, as_new=False)
            if check.reason:
                issues.append(check.reason)
        return issues
