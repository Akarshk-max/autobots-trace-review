"""The context vector - one result object per redirecting node, per graph node.

In a DAG "the context of node n" is a single value, because n fires once. Once n can be re-entered
from a cycle it stops being a function of n alone: it depends on *who sent it there this time*. So
each node carries a vector of slots, indexed by the identity of the redirecting node.

    V_n = [ V_n0, V_n1, ... ]      V_ni = the context n should have when the redirection into it
                                          is due to node i

Slot 0 is the baseline - the pre-redirection dimension, and the only one a user ever talks to. An
acyclic graph has K = 1, so it has slot 0 and nothing else, which is exactly today's behaviour.

A slot does not hold one value; it holds an ordered, tagged transcript, because once results merge
it is no longer one node's turn but an interleaved record of several.

This module is the storage model, the merge rule, and - since Part 4 - the rendering that turns a
transcript back into prompt text. `linearise_slot` is the read half: Parts 1-3.5 made a slot
correct and left it write-only, so a re-entered node had a perfect record of its own history and
could not see a word of it.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from loguru import logger
from pydantic import BaseModel, Field

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.core.logging.app_code import AppCode

# Slot keys are strings because they are Mongo document keys.
BASELINE_SLOT = "0"


class ContextEntry(BaseModel):
    """One tagged contribution to a slot's transcript.

    `node_id` is *provenance*, not location: the start node's id for the forward pass, and the
    redirecting node's id for each redirect. Those two sets are provably disjoint - the start node
    is the data-DAG root, so it is a descendant of nothing and can never be a redirect source -
    which is what lets a bare tag say unambiguously which kind of entry this is.
    """

    input: Optional[Dict[str, Any]] = None
    output: Optional[Dict[str, Any]] = None
    node_id: str = Field(description="provenance tag: the start node, or the redirecting node")
    turn_no: int = Field(
        default=0,
        ge=0,
        description="counter per (slot, node_id), from 0 - independent per tag and sparse",
    )

    def identity(self) -> Tuple[int, str]:
        """What makes two entries the same. `turn_no` counts per tag, so the pair is unique."""
        return (self.turn_no, self.node_id)


def sort_key(entry: ContextEntry, start_node_id: Optional[str]) -> Tuple[int, int, str]:
    """Chronological by turn; within a turn the start node's conversation leads.

    The explicit rank is not cosmetic. Node ids are digit strings and the start node's id is a
    letter, so plain lexicographic ordering would sort the start node *last* ('0' is 0x30, 'a' is
    0x61) - the opposite of what is wanted.
    """
    is_start = start_node_id is not None and entry.node_id == start_node_id
    return (entry.turn_no, 0 if is_start else 1, entry.node_id)


class ContextSlot(BaseModel):
    """V_ni - an ordered, tagged transcript. Kept sorted; every operation returns a new slot."""

    entries: List[ContextEntry] = []

    def next_turn_no(self, node_id: str) -> int:
        """The turn a new entry for this tag takes. Counters advance per tag, independently.

        There is no global lap number: a transcript may legitimately hold ("2", 0), ("2", 1) and
        ("2", 2) alongside only (start, 0).
        """
        turns = [e.turn_no for e in self.entries if e.node_id == node_id]
        return max(turns) + 1 if turns else 0

    def sorted_entries(self, start_node_id: Optional[str]) -> List[ContextEntry]:
        return sorted(self.entries, key=lambda e: sort_key(e, start_node_id))

    def tagged(self, node_id: str) -> List[ContextEntry]:
        return [e for e in self.entries if e.node_id == node_id]


def merge_slots(
    existing: ContextSlot,
    incoming: ContextSlot,
    *,
    start_node_id: Optional[str] = None,
) -> ContextSlot:
    """f_comb - fold an incoming transcript into an existing one.

    Pure addition. Straight assignment is wrong: a redirect does not erase what the node already
    knew along its forward path, so nothing is ever overwritten and nothing is dropped. Entries are
    identified by (turn_no, node_id); where both sides carry the same identity the existing entry
    wins, since the incoming one cannot be newer information about an already-recorded turn.
    """
    merged: List[ContextEntry] = list(existing.entries)
    seen = {e.identity() for e in merged}
    for entry in incoming.entries:
        if entry.identity() not in seen:
            seen.add(entry.identity())
            merged.append(entry)
    return ContextSlot(entries=sorted(merged, key=lambda e: sort_key(e, start_node_id)))


def append_entry(
    slot: ContextSlot,
    *,
    node_id: str,
    input: Optional[Dict[str, Any]] = None,
    output: Optional[Dict[str, Any]] = None,
    start_node_id: Optional[str] = None,
) -> ContextSlot:
    """Add one contribution, taking the next turn number for its tag."""
    entry = ContextEntry(
        input=input, output=output, node_id=node_id, turn_no=slot.next_turn_no(node_id)
    )
    return merge_slots(slot, ContextSlot(entries=[entry]), start_node_id=start_node_id)


def upsert_entry(
    slot: ContextSlot,
    *,
    node_id: str,
    turn_no: int,
    input: Optional[Dict[str, Any]] = None,
    output: Optional[Dict[str, Any]] = None,
    start_node_id: Optional[str] = None,
) -> ContextSlot:
    """Write one execution's result into a fixed (node_id, turn_no) cell, replacing what is there.

    The only writer that replaces, and it exists for one reason: a loop node reports
    *progressively*. The engine records the collated ActionDoc once per iteration, and the whole
    fan-out is a single entry into the node. So:

      - `append_entry` here would produce N entries for one execution, multiplying the payload by N
        and making a 200-row loop look like 200 re-entries;
      - `merge_slots` here would keep the first, partial collation and discard the final one.

    Neither is a bug in those functions - they are simply for a different job. Do not "simplify"
    this call site into one of them.
    """
    entry = ContextEntry(input=input, output=output, node_id=node_id, turn_no=turn_no)
    others = [e for e in slot.entries if e.identity() != entry.identity()]
    return ContextSlot(entries=sorted([*others, entry], key=lambda e: sort_key(e, start_node_id)))


def _indent(text: str, indent: str) -> str:
    """Indent every line of a possibly multi-line string, matching `to_input`'s own helper."""
    return "\n".join(f"{indent}{line}" for line in (text or "").splitlines())


def provenance_label(node_id: str, start_node_id: Optional[str]) -> str:
    """How an entry's tag reads in a prompt.

    The tag namespace is provably disjoint (SS1.7.1) - the start node is the data-DAG root, so it
    can never be a redirect source - which is what lets a bare tag say unambiguously which kind of
    entry this is, with no discriminator field to consult.
    """
    if start_node_id is not None and node_id == start_node_id:
        return "initial pass"
    return f"redirected by node {node_id}"


def entry_label(entry: "ContextEntry", start_node_id: Optional[str]) -> str:
    """How one entry announces itself in a prompt.

    One definition, shared by the flat rendering here and by the per-turn `ActionResult` shape
    `context_input` builds, so the two cannot drift into describing the same turn differently.
    """
    return f"Turn {entry.turn_no} - {provenance_label(entry.node_id, start_node_id)}"


class LinearisedSlot(BaseModel):
    """A slot rendered for a prompt: the transcript as text, plus every url it referenced.

    Two fields because a DataBlock carries urls beside its text and the engine attaches files from
    them. Folding the urls into the text would lose them - a re-entered node would see a filename
    it has no way to open.
    """

    text: str = ""
    urls: List[str] = []

    @property
    def is_empty(self) -> bool:
        return not self.text and not self.urls


def linearise_slot(
    slot: ContextSlot,
    *,
    start_node_id: Optional[str],
    include_input: bool = False,
) -> LinearisedSlot:
    """Re-linearise a tagged transcript into prompt text - the read half of the context vector.

    Parts 1-3.5 made a slot correct and left it write-only: `to_input` builds every prompt from
    `output[parent]`, the flat baseline mirror, so a re-entered node has a perfect record of its
    own history and cannot see a word of it. This is the function that turns the record back into
    something a model can read.

    **Ordering is the slot's, not this function's.** `sorted_entries` applies SS1.7's sort key -
    chronological by turn, start node first within a turn. This walks that order and does not
    re-sort, so there is one definition of "what came first" rather than two that can drift.

    **Turn numbers are printed, not counted.** They are sparse by construction: a node only takes
    part in the turns whose `R.S.` contains it, so a transcript legitimately reads 0, 2, 3 with no
    turn 1, and an abandoned turn (RISK-12) leaves a hole as well. Numbering the entries 1..N
    instead would silently renumber a gap away and tell the model a history it did not have.

    **Tolerant of a malformed cell.** An entry whose payload does not validate is skipped rather
    than raised on: prompt composition sits in the middle of a run the user has already paid for,
    and one unreadable turn should cost that turn, not the run. Same posture as `to_input`, which
    wraps every upstream in `try`.

    `include_input` renders each turn's input beside its output - what
    `DataFlowType.both_input_output` asks for. Off by default, because the input of a re-entry is
    largely the previous turn's output and printing both doubles the prompt for little.
    """
    lines: List[str] = []
    urls: List[str] = []

    for entry in slot.sorted_entries(start_node_id):
        try:
            body: List[str] = []
            if include_input and entry.input is not None:
                block = DataBlock.model_validate(entry.input)
                body.append("**Input:**")
                body.append(_indent(block.text or "", "    "))
                urls.extend(block.urls or ())

            blocks = DataBlocks.model_validate(entry.output or {}).blocks
            if body:
                body.append("")
            body.append("**Output:**")
            for block in blocks:
                body.append(_indent(block.text or "", "    "))
                urls.extend(block.urls or ())
        except Exception:
            # A cell that will not validate is one turn of history lost, not a failed run.
            logger.bind(code=AppCode.ACTION_GRAPH).exception(
                f"Skipping an unreadable context entry (turn {entry.turn_no}, "
                f"tag {entry.node_id!r}) while composing input"
            )
            continue

        if lines:
            lines.append("")
        lines.append(
            f"### {entry_label(entry, start_node_id)}"
        )
        lines.extend(body)

    # Deduplicate while keeping order: the same file referenced by three turns is one attachment,
    # and the attachment limit is counted in urls.
    seen: set[str] = set()
    unique_urls = [u for u in urls if not (u in seen or seen.add(u))]
    return LinearisedSlot(text="\n".join(lines), urls=unique_urls)


class NodeContextVector(BaseModel):
    """V_n. Sparse - only populated slots are stored, so a DAG node carries just the baseline."""

    slots: Dict[str, ContextSlot] = {}

    def slot(self, context_index: int | str) -> ContextSlot:
        return self.slots.get(str(context_index), ContextSlot())

    @property
    def baseline(self) -> ContextSlot:
        return self.slot(BASELINE_SLOT)

    def with_slot(self, context_index: int | str, slot: ContextSlot) -> "NodeContextVector":
        return NodeContextVector(slots={**self.slots, str(context_index): slot})


def entry_from_action_doc(
    action_doc: ActionDoc, *, node_id: str, turn_no: int = 0
) -> ContextEntry:
    """One execution of a node becomes one entry.

    Not one per `ActionResult`: a loop node fans out into many parallel runs that are collated back
    into a single ActionDoc, and that is still a single entry into the node. The R.O. is the node's
    input and output for that execution.
    """
    return ContextEntry(
        input=action_doc.input,
        output=action_doc.output,
        node_id=node_id,
        turn_no=turn_no,
    )


def baseline_vector(action_doc: ActionDoc, *, start_node_id: str) -> NodeContextVector:
    """The vector for a node that has run once on the forward pass and never been re-entered."""
    entry = entry_from_action_doc(action_doc, node_id=start_node_id)
    return NodeContextVector(slots={BASELINE_SLOT: ContextSlot(entries=[entry])})


def seed_all_slots(
    action_doc: ActionDoc, *, start_node_id: str, slot_keys: Iterable[int | str]
) -> NodeContextVector:
    """Case (i): nothing has redirected yet, so write the same R.O. into every slot.

    Not waste - it establishes a baseline. Whoever redirects here later, the pre-cycle history is
    identical for all of them, so every dimension starts from the same place. It is also what makes
    "reset this column to the baseline" coherent when a subgraph is re-run.
    """
    entry = entry_from_action_doc(action_doc, node_id=start_node_id)
    keys = {str(k) for k in slot_keys} | {BASELINE_SLOT}
    return NodeContextVector(
        slots={key: ContextSlot(entries=[entry.model_copy(deep=True)]) for key in sorted(keys)}
    )


def baseline_matches_action_doc(
    vector: NodeContextVector, action_doc: ActionDoc, *, start_node_id: str
) -> bool:
    """Is the baseline slot still the projection of the ActionDoc stored in `output`?

    `output[n]` remains where the ActionDoc lives - the vector cannot replace it, since an entry
    carries no name, type or config. The baseline slot is a tagged *projection* of that same
    execution, and the two must not drift.

    **Every baseline entry counts, not just the start node's.** Slot 0 used to be filtered to
    `tagged(start_node_id)`, on the assumption that only the forward pass ever writes there. A
    continue-run breaks that: it re-executes a subtree on the baseline column and tags its entries
    with the node it was started from, so the highest-turn entry is no longer start-tagged. Under
    the old filter the check compared the *forward pass's* output to a freshly re-run
    `output[n]` and reported drift on every node of every continue-run.

    So slot 0 is now read as what it is - the baseline **column**, one ordered sequence - rather
    than as the start node's private lane. `start_node_id` is kept in the signature because every
    caller has it and the argument is part of this module's shape, but the comparison no longer
    needs it.
    """
    entries = vector.baseline.entries
    if not entries:
        return False
    latest = max(entries, key=lambda e: e.turn_no)
    return latest.input == action_doc.input and latest.output == action_doc.output
