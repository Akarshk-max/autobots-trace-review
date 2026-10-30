"""The single place a node's result is written into a run.

A node's result lives in two structures that must agree:

  - `result.output[node]` - the ActionDoc. The *executable* (id, name, type, config) plus the
    **baseline** record (input, output, results), which every existing consumer reads.
  - `result.node_vectors[node]` - the context vector, holding the same execution tagged by
    provenance and indexed by redirecting node.

**`output[node]` is the baseline, not the latest.** A re-entry writes only the dimensions its turn
disturbs, and does not touch `output` - so after a cycle the map still holds the forward pass, and
a turn's answer is read from its slot. The two are a mirror precisely because the write is scoped
this way; writing every execution there instead made them disagree the first time a node returned
a different answer on a re-entry, which `assert_baseline_mirror` reported and nothing acted on.

Readers only ever touch the first, so a missed vector write leaves `output` correct and the vector
stale - invisible to every consumer until the scheduler reads a slot and gets the wrong context.
That asymmetry is why every write goes through `record_node_result` rather than assigning
`result.output[node]` directly, and why `tests/graphs/test_baseline_mirror.py` fails the build if a
new direct assignment appears.

This does not live in `context_vector.py` (that module is deliberately pure, and
`action_graph_doc_model` imports it - depending on `ActionGraphResultDoc` from there would cycle),
nor on `UserActionGraphResult` (the invariant has to hold *between* saves, and that class cannot
know which node just ran).
"""

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from loguru import logger

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action_graph.action_graph.context_vector import (
    BASELINE_SLOT,
    NodeContextVector,
    baseline_matches_action_doc,
    upsert_entry,
)
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
)
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context


@dataclass(frozen=True)
class NodeRunContext:
    """Everything the writer needs about one execution of one node.

    Resolved before the node runs and held constant across a loop node's iterations - that is what
    makes a 200-row fan-out land in a single cell instead of 200.
    """

    node: str

    # Only consulted when a vector is actually written, so a DAG can leave it empty.
    start_node_id: str = ""

    # Provenance: the start node on the forward pass, the redirecting node on a re-entry.
    # Defaults to the start node so a DAG needs no ceremony.
    tag: str = ""

    # Counts per (slot, tag), independently and sparsely. There is no global turn number.
    turn_no: int = 0

    # Which slot this execution *reads*. Not the same question as which it writes: a turn writes
    # every dimension it disturbed, and reads exactly one. See `context_input`.
    context_index: str = BASELINE_SLOT

    # Is this node the target of the redirect that opened this turn? The target reads its own slot
    # plus the redirector's handback; every other participant reads its data parents' slots. One
    # rule per role - see `context_input`.
    is_redirect_target: bool = False

    # The node's data parents, kept so the target's read has something to fall back to on the very
    # first re-entry, before it has any history of its own.
    data_parents: Tuple[str, ...] = field(default=())

    # Which slots this execution writes into. EMPTY MEANS NO VECTOR AT ALL - a DAG (K == 1) keeps
    # `node_vectors` None so its run document stays byte-identical to one produced before cyclic
    # support existed. Case (i) passes every slot; case (ii) passes just the one.
    target_slots: Tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        # A tag is the start node or a redirecting node - never the node being executed. Without
        # this, a context built with slots but no tag writes entries tagged "", which nothing
        # downstream would flag and which no slot lookup would ever match.
        if self.target_slots and not self.provenance_tag:
            raise ValueError(
                f"Node {self.node!r} writes context slots but has no provenance tag. Pass "
                "start_node_id for a forward pass, or tag=<redirecting node> for a re-entry."
            )

    @property
    def provenance_tag(self) -> str:
        """Who caused this execution: the start node on the forward pass, else the redirector.

        Never `self.node` - the entry records where the context came from, not where it landed.
        """
        return self.tag or self.start_node_id

    @classmethod
    def for_dag(cls, node: str) -> "NodeRunContext":
        """A node in an acyclic graph: baseline only, no vector, byte-identical to before."""
        return cls(node=node)


def _writable_graph(result_doc: ActionGraphResultDoc):
    """The run's graph, asserted to be the full document.

    `ActionGraphResultDoc.result` is `ActionGraphDoc | ActionGraphLiteDoc`, and the lite shape - the
    Mongo projection - has neither `output` nor `node_vectors`. The engine always holds the full
    doc, but callers reach it through a union, and a lite doc here would otherwise surface as an
    AttributeError that `empty_downstream_nodes`' broad `except` swallows into "emptied nothing".
    """
    graph = result_doc.result
    if not hasattr(graph, "output"):
        raise TypeError(
            f"Run {result_doc.id!r} carries a lite result ({type(graph).__name__}); node results "
            "can only be written to a full ActionGraphDoc."
        )
    return graph


def record_node_result(
    result_doc: ActionGraphResultDoc,
    action_doc: ActionDoc,
    run_ctx: NodeRunContext,
) -> ActionDoc:
    """Store a node's result in both structures. The only supported way to write one.

    `output[node]` is written only by an execution that targets the **baseline** slot - a DAG,
    which targets no slots at all, or case (i), which seeds every slot. A re-entry writes its own
    dimensions and leaves the baseline alone. See the module docstring for why.

    Returns the stored ActionDoc so a call site stays a single expression.
    """
    graph = _writable_graph(result_doc)
    if graph.output is None:
        graph.output = {}
    stored = ActionDoc.model_validate(action_doc)

    # `output[node]` is the BASELINE record, not "whatever ran most recently". A re-entry writes
    # its own dimension and must leave the baseline alone: the per-turn values are in
    # `node_vectors` and are read from there by whoever wants them.
    #
    # Writing every execution here made `output[node]` and slot 0 disagree the moment a cycle
    # produced a different answer - `assert_baseline_mirror` fired on every re-entry of a healthy
    # run. It stayed invisible because the guard is a canary and because a deterministic action
    # makes the comparison pass vacuously; `test_baseline_mirror_under_a_cycle.py` is the test
    # that does not let it.
    #
    # It also fixes what a node outside `R.S.` contributes. Case (ii) says it hands on the value
    # it already has, and `contextual_input` gets that from `output[parent]` - which used to hold
    # a value from whichever dimension ran last, not the parent's baseline.
    #
    # `propagation_slots` can never contain BASELINE_SLOT (context indices start at 1 and the
    # start node is never an R.N.), so a redirect turn structurally cannot reach this branch. The
    # writers are: a DAG (no slots at all), and case (i), which seeds every slot including 0.
    if not run_ctx.target_slots or BASELINE_SLOT in run_ctx.target_slots:
        graph.output[run_ctx.node] = stored

    if not run_ctx.target_slots:
        return stored

    if graph.node_vectors is None:
        graph.node_vectors = {}
    vector = graph.node_vectors.get(run_ctx.node) or NodeContextVector()
    for slot_key in run_ctx.target_slots:
        vector = vector.with_slot(
            slot_key,
            upsert_entry(
                vector.slot(slot_key),
                node_id=run_ctx.provenance_tag,
                turn_no=run_ctx.turn_no,
                input=stored.input,
                output=stored.output,
                start_node_id=run_ctx.start_node_id,
            ),
        )
    graph.node_vectors[run_ctx.node] = vector
    return stored


def seed_baseline_from_output(
    result_doc: ActionGraphResultDoc,
    *,
    start_node_id: str,
    all_slots: Tuple[str, ...],
) -> List[str]:
    """Reconstruct the vectors a run would have had, for a run that predates them.

    **Why this is needed at all.** `is_node_done` answers "output but no vector" with *not done*,
    because at K > 1 that combination is drift - some write used `for_dag` where it should have
    carried slots. On a document written before vectors existed it means something else entirely:
    the node ran, before the deploy. Without this the engine reads every completed node as never
    having run and re-executes it, in billed model calls, on the first resume or continue-run
    after the change. That is the whole failure this function exists to prevent.

    **Lazy, not a migration.** It runs when a run is next touched, so historical documents nobody
    opens again cost nothing, there is no S3 sweep to schedule and no race with a run in flight.
    Idempotent by construction: `node_vectors is not None` is the "already done" signal.

    Precedent: Part 3.5 kept `_turns_recorded` for exactly this shape - the transcript is the only
    record of what a run did, so it *seeds* the machine rather than the machine concluding the run
    did nothing.

    **What it reconstructs.** `ActionDoc.results` is a clean per-run execution log: an action's
    stored `results` is always empty (`ActionUpdate` carries no `results` field, so no update can
    write one), and the graph appends one entry per execution. So entry *i* becomes turn *i*.

    Two details make the result honest rather than merely present:

      - **Every turn goes into every slot**, because every execution `results` records did.
        `record_node_result` writes `output[node]` only when the baseline slot is targeted, and a
        redirect turn targets `propagation_slots`, which never contains slot 0 - so a redirect
        turn never appears in `results` at all. Measured: a node that executed four times, once
        forward and three times round the loop, holds **one** result.

        What `results` therefore holds is exactly the baseline-column executions - the forward
        pass, continue-runs and chats - and every one of those writes every slot. Seeding turn 0
        broadly and later turns narrowly would be borrowing the redirect-turn rule for entries no
        redirect turn produced.
      - **The final turn is built from the `ActionDoc`, not from `results[-1]`.** They should
        agree, but `baseline_matches_action_doc` compares the highest-turn entry against the
        document, so sourcing it from the document makes the mirror hold by construction instead
        of by assumption.

    Returns the nodes seeded, for logging. Does nothing - and returns `[]` - when there is already
    a vector, when nothing has run, or when the graph has no start node to tag entries with.
    """
    graph = _writable_graph(result_doc)
    output = graph.output or {}
    if graph.node_vectors is not None or not output or not start_node_id:
        return []

    slots = tuple(all_slots) or (BASELINE_SLOT,)
    seeded: Dict[str, NodeContextVector] = {}
    for node, action_doc in output.items():
        history = list(action_doc.results or ())
        pairs = [(entry.input, entry.output) for entry in history[:-1]]
        pairs.append((action_doc.input, action_doc.output))

        vector = NodeContextVector()
        for turn_no, (entry_input, entry_output) in enumerate(pairs):
            for slot_key in slots:
                vector = vector.with_slot(
                    slot_key,
                    upsert_entry(
                        vector.slot(slot_key),
                        node_id=start_node_id,
                        turn_no=turn_no,
                        input=entry_input,
                        output=entry_output,
                        start_node_id=start_node_id,
                    ),
                )
        seeded[node] = vector

    graph.node_vectors = seeded
    return sorted(seeded)


def forget_node(result_doc: ActionGraphResultDoc, node: str) -> None:
    """Drop a node's result from both structures.

    Deletion is where a mirror is easiest to forget: a node popped from `output` but left in
    `node_vectors` is drift no per-node check can see, because there is nothing left to compare.
    """
    graph = _writable_graph(result_doc)
    if graph.output:
        graph.output.pop(node, None)
    if graph.node_vectors:
        graph.node_vectors.pop(node, None)


def forget_all(result_doc: ActionGraphResultDoc) -> None:
    """Reset a run to nothing executed - a full rerun, or a freshly initialised run.

    Empties the maps **in place** rather than replacing them. `node_results` hands out the live
    dict, and the engine binds it once per function; rebinding `graph.output` to a fresh `{}` would
    leave every such binding pointing at a detached dict that later writes never reach. Today every
    `forget_all` runs before the engine takes that binding, so replacing would also work - but only
    by ordering, and Part 3 re-enters nodes mid-run. Clearing in place holds regardless of order.
    """
    graph = _writable_graph(result_doc)
    if graph.output is None:
        graph.output = {}
    else:
        graph.output.clear()
    if graph.node_vectors is not None:
        graph.node_vectors.clear()


def node_results(result_doc: ActionGraphResultDoc) -> Dict[str, ActionDoc]:
    """The live per-node map. Read through this rather than holding a binding.

    `update_action_graph_result` re-reads the run from S3 and rebinds the doc, so a reference taken
    once can end up pointing at a detached dict.
    """
    graph = result_doc.result
    if graph.output is None:
        graph.output = {}
    return graph.output


def is_node_done(
    result_doc: ActionGraphResultDoc,
    node: str,
    *,
    context_index: int | str = BASELINE_SLOT,
    tag: Optional[str] = None,
    turn_no: Optional[int] = None,
) -> bool:
    """Has `node` already produced the result being asked for?

    Three readings, narrowing as more is specified:

    **K == 1** (`node_vectors is None`) - reduces to "is the key present in `output`", exactly the
    check the layer walk has always made. Nothing about a DAG's skip decisions changes, which is
    the property the whole branch rests on.

    **K > 1, no tag/turn** - "has this node recorded anything for this context?" The coarse
    readiness question: has an upstream delivered into the slot we are about to read.

    **K > 1, tag and turn given** - "has *this execution* been recorded?" Identity is
    `(turn_no, tag)`, the same pair `ContextEntry.identity` uses. This is the re-armable reading:
    a fresh turn carries a fresh `turn_no`, so a node that was done for the last turn is not done for
    this one. Without it, "done" stays a permanent property of the node and re-entry is impossible
    - which is precisely why the current engine cannot cycle.
    """
    if (tag is None) != (turn_no is None):
        raise ValueError(
            "tag and turn_no identify one execution together - pass both or neither"
        )

    graph = result_doc.result
    output = getattr(graph, "output", None) or {}

    vectors = getattr(graph, "node_vectors", None)
    if vectors is None:
        # K == 1. Presence in `output` is the whole answer.
        return node in output

    # Deliberately NOT gated on `node in output` first. That gate used to double as "has this node
    # ever run", which only held while every execution wrote `output[node]`. Now that the map is
    # the baseline record, a node that has so far run only inside a turn has a vector and no
    # output entry, and it is done for the slot it wrote. Asking `output` here would report every
    # such node as never-run and re-run it.
    vector = vectors.get(node)
    if vector is None:
        # Output without a vector in a cyclic graph is drift - some write used `for_dag` where it
        # should have carried slots. Answer "not done" so the node re-runs rather than being
        # silently skipped; `assert_vector_covers_output` is what reports the drift itself.
        return False

    slot = vector.slot(context_index)
    if not slot.entries:
        return False
    if tag is None:
        return True

    if str(context_index) == BASELINE_SLOT:
        # The baseline column is ORDERED, not tagged - see `baseline_matches_action_doc`. It is a
        # single run-wide sequence: the forward pass writes turn 0 under the start node, and each
        # continue-run appends the next turn under the node it was started from. So "have you done
        # this turn" is `have you reached it`, not `do you hold this exact (turn, tag)` - the tag
        # differs between the forward pass and a continue-run, and matching on it would report
        # every node as never having run this turn.
        #
        # Ordering also gives the right answer when turn numbers are sparse: a node that sat out
        # turn 1 and is asked about turn 2 is correctly not done, and one already at turn 2 asked
        # again is correctly done - which is what stops a continue-run running a node twice.
        #
        # Slots 1..K-1 keep exact identity. Their turns count per tag and interleave, so ordering
        # there would let one redirector's turn satisfy another's.
        return max(entry.turn_no for entry in slot.entries) >= turn_no

    return any(entry.identity() == (turn_no, tag) for entry in slot.entries)


def assert_baseline_mirror(
    ctx: Context,
    result_doc: ActionGraphResultDoc,
    node: str,
    *,
    start_node_id: Optional[str] = None,
    strict: bool = False,
) -> bool:
    """Has `output[node]` drifted from its baseline slot?

    Call once per node, never per loop iteration - the deep payload comparison is the expensive
    part, and only the last iteration's write matters. Logs by default and raises only under
    `strict`: drift is a data-quality bug, not a reason to fail a run the user has already paid for.
    """
    graph = result_doc.result
    vector = (graph.node_vectors or {}).get(node)
    if vector is None or not start_node_id:
        return True  # a DAG keeps no vector, so there is nothing to drift from

    action_doc = (graph.output or {}).get(node)
    if action_doc is None:
        return True  # covered by the whole-run check below, which sees deletions

    matches = baseline_matches_action_doc(vector, action_doc, start_node_id=start_node_id)
    if not matches:
        logger.bind(
            ctx=ctx,
            code=AppCode.ACTION_GRAPH_RUN,
            action_graph_run_id=result_doc.id,
            node_id=node,
        ).error("Baseline mirror drift: output[node] and slot 0 disagree")
        if strict:
            raise AssertionError(f"Baseline mirror drift on node {node!r}")
    return matches


def assert_upstream_turn_recorded(
    ctx: Context,
    result_doc: ActionGraphResultDoc,
    node: str,
    *,
    parents: Iterable[str],
    context_index: int | str,
    tag: str,
    turn_no: int,
    strict: bool = False,
) -> bool:
    """Has every parent that takes part in this turn already recorded it?

    The precise upstream check §2.5 asks for: before `node` runs at turn `turn_no`, every parent of
    it that is in `R.S.(i, j)` should hold an entry `(tag=j, turn=turn_no)` in the turn's slot. The
    caller supplies `parents` already intersected with `R.S.` - a parent outside it does not take
    part in the turn and correctly holds nothing new.

    **A canary, not a gate.** It cannot fire today: `rank` is topological and execution inside a
    turn is sequential, so a parent has always run by the time its child does. As a gate it would be
    dead code that could only ever be wrong. It becomes load-bearing the moment anything inside a
    turn runs concurrently - and a check that has been quietly correct for months is worth more than
    one written under pressure afterwards.

    So it logs and continues, raising only under `strict`, exactly like `agree()` and the two
    mirror guards. A canary that takes down production runs is a worse canary.
    """
    missing = [
        parent
        for parent in parents or ()
        if not is_node_done(
            result_doc, parent, context_index=context_index, tag=tag, turn_no=turn_no
        )
    ]
    if not missing:
        return True

    logger.bind(
        ctx=ctx,
        code=AppCode.ACTION_GRAPH_RUN,
        action_graph_run_id=result_doc.id,
        node_id=node,
        context_index=str(context_index),
        tag=tag,
        turn_no=turn_no,
        missing_upstream=sorted(missing),
    ).error(
        "A node is running a turn its relevant-subgraph parents have not recorded yet - the "
        "within-turn ordering (§2.5) is broken"
    )
    if strict:
        raise AssertionError(
            f"Node {node!r} runs turn (tag={tag!r}, turn={turn_no}) in slot "
            f"{str(context_index)!r} before {sorted(missing)} recorded it"
        )
    return False


def assert_vector_covers_output(
    ctx: Context, result_doc: ActionGraphResultDoc, *, strict: bool = False
) -> bool:
    """Do the two structures hold the same node set?

    O(nodes) with no payload comparison, so it is cheap enough to leave on. This is the half that
    catches a deletion applied to one structure and not the other.
    """
    graph = result_doc.result
    if graph.node_vectors is None:
        return True

    output_nodes = set(graph.output or {})
    vector_nodes = set(graph.node_vectors)
    if output_nodes == vector_nodes:
        return True

    logger.bind(
        ctx=ctx,
        code=AppCode.ACTION_GRAPH_RUN,
        action_graph_run_id=result_doc.id,
        only_in_output=sorted(output_nodes - vector_nodes),
        only_in_vectors=sorted(vector_nodes - output_nodes),
    ).error("Baseline mirror drift: output and node_vectors cover different nodes")
    if strict:
        raise AssertionError("output and node_vectors cover different nodes")
    return False


__all__ = [
    "BASELINE_SLOT",
    "NodeRunContext",
    "assert_baseline_mirror",
    "assert_upstream_turn_recorded",
    "assert_vector_covers_output",
    "forget_all",
    "forget_node",
    "is_node_done",
    "node_results",
    "record_node_result",
    "seed_baseline_from_output",
]
