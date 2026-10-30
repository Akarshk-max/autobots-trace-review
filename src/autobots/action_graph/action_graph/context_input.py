"""The read path: what a node sees when it runs, for the context it is running under.

`node_result_writer` is the write half - one node's result into `output[n]` and `node_vectors[n]`.
This is the other half, and until Part 4 it did not exist: `to_input` built every prompt from
`output[parent]`, the flat baseline mirror, so a re-entered node had a perfect transcript of its
own history and could not see a word of it.

### Reading is two rules, by role - not one

This is the thing to get right, and the easy mistake is to write a single rule:

  - **The redirect target** reads **its own slot** plus **the input the redirector handed back**.
    When it has no data parents at all - the root of the graph, sent work back to it - it never
    reaches `to_input`, so the same context goes in through the graph input instead:
    `contextual_input_dict`. Same rule, different delivery.
    It does *not* re-read its data parents: they sit upstream of the loop and have nothing new to
    say. Node -1 in SS1.6 did not run this turn, and re-reading it would hand the node being told
    to try again a stale value and none of the feedback. This is SS1.4's case (ii) literally:
    `{ V_ik } + k_input -> i -> i_output`.
  - **Every other participating node** reads **its data parents' slots** for this context, one hop,
    as it always has. A node three hops down the loop has no business seeing its grandparent's
    output; it should know exactly what its own parent handed it.

`k_input` - the handback - is where Part 5's redirect message ("go back and fix the tone") lives.
The channel is built here and nothing writes to it yet, so today the target reads its own transcript
and nothing else.

### `to_input` keeps its signature

It takes `upstream_nodes: List[str]` and `action_response: Dict[str, ActionDoc]`, and it keeps
taking exactly those. What changes is *what it is handed*: the parent list and the document map are
both composed here first. A parent's `ActionDoc` is replaced by a *context view* of itself - the
same document with its transcript for this context in place of its single latest result - and for
the target the list is replaced outright by itself plus the redirector.

Threading a slot into `to_input` instead would reach all four of its branches (three read
`action_doc.output`, one reads `action_doc.results`) and every run path that calls it. Composing
here touches none of them, and it makes the K == 1 safety property trivial rather than argued: with
no `node_vectors` this returns the caller's own list and the live `node_results` map *by identity*,
so a DAG cannot compose a different prompt however hard it tries.

Same shape Part 3.5 chose for OQ-9: the caller transforms, `to_input` stays ignorant of turns.

### The baseline is deliberately not read through the transcript

Slot 0 is a *mirror* of `output[n]` - `baseline_matches_action_doc` is the invariant that pins it -
so linearising it would add no information and change the text. Reading it through the transcript
would also make a cyclic graph's forward pass compose a different prompt from the identical DAG's,
which is a property worth more than the uniformity: a graph's first pass reads the same either way,
and only a genuine re-entry sees a transcript.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.autobots.action.action.action_doc_model import ActionDoc, ActionResult
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action_graph.action_graph.context_vector import (
    BASELINE_SLOT,
    ContextSlot,
    NodeContextVector,
    entry_label,
    linearise_slot,
)
from src.autobots.action_graph.action_graph.edge_decision import message_for
from src.autobots.action_graph.action_graph.node_result_writer import (
    NodeRunContext,
    node_results,
)
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
)


def _tagged_results(slot: ContextSlot, start_node_id: Optional[str]) -> List[ActionResult]:
    """One `ActionResult` per turn, in the slot's own order, each labelled with its provenance.

    The label goes into the first block's text rather than into a field of its own, because
    `ActionResult` has exactly `input` and `output` and gaining a field would change a model every
    stored action already uses. It reads as a header line above the turn's output.

    Ordering is `sorted_entries`, walked and not re-derived - one definition of "what came first".
    """
    results: List[ActionResult] = []
    for entry in slot.sorted_entries(start_node_id):
        label = f"[{entry_label(entry, start_node_id)}]"
        try:
            blocks = DataBlocks.model_validate(entry.output or {})
        except Exception:
            # Same posture as `linearise_slot`: one unreadable cell costs its turn, not the run.
            continue
        if blocks.blocks:
            first = blocks.blocks[0]
            first.text = f"{label}\n{first.text or ''}"
        else:
            blocks.blocks = [DataBlock(text=label)]
        results.append(ActionResult(input=entry.input, output=blocks.model_dump()))
    return results


def context_view(
    action_doc: ActionDoc,
    slot: ContextSlot,
    *,
    start_node_id: Optional[str],
    name: Optional[str] = None,
) -> ActionDoc:
    """`action_doc` as it should read for one context - its transcript, not its latest result.

    A copy: the stored `ActionDoc` in `output[n]` is the baseline record every existing consumer
    reads, and rewriting it in place would corrupt the mirror `assert_baseline_mirror` guards.

    Fills **both** `results` and `output`, because `to_input`'s branches read different fields: the
    default branch walks `results` and already renders "### Run N / **Output:**", so the transcript
    reuses that scaffolding; the three loop branches validate `output` as `DataBlocks` and have
    nowhere to put per-turn structure, so there the turns are carried in the text.

    `name` overrides the heading `to_input` prints for this section - used to label the target's own
    history as its own rather than as an upstream's.
    """
    view = action_doc.model_copy(deep=True)
    view.results = _tagged_results(slot, start_node_id)
    rendered = linearise_slot(slot, start_node_id=start_node_id)
    view.output = {"blocks": [{"text": rendered.text, "urls": rendered.urls}]}
    if name:
        view.name = name
    return view


def _handback_view(action_doc: ActionDoc, message: str) -> ActionDoc:
    """The redirector's message to its target, shaped as an upstream contribution.

    Attributed to the **redirector**, because that is who said it. It carries no transcript - it is
    one instruction for one turn, not a history.
    """
    view = action_doc.model_copy(deep=True)
    view.results = [ActionResult(input=None, output={"blocks": [{"text": message}]})]
    view.output = {"blocks": [{"text": message}]}
    view.name = f"{action_doc.name or 'redirect'} (sent this back to you)"
    return view


def contextual_input_dict(
    result_doc: ActionGraphResultDoc,
    node: str,
    input_dict: Dict[str, Any],
    *,
    run_ctx: NodeRunContext,
) -> Dict[str, Any]:
    """The graph input a node should run on - for a redirect target that has **no data parents**.

    The root of a graph can be a redirect target: "go back to the beginning and try again" is the
    most ordinary loop anyone draws, and SS1.7.1 confirms `j` ⇢ root is structurally valid. Such a
    node has an empty parent list, so it never reaches `to_input` at all - `run_action_simple`, the
    two split-loop paths and `run_action_with_excel_loop` take `action_graph_input_dict` straight to
    the action.

    Without this it re-runs on **byte-identical input every turn** while its slot faithfully records
    each one: it records its history and never reads it, which is exactly the limitation this part
    exists to remove. Worse than an ordinary node's version of the same bug, because the loop still
    burns its whole budget producing the same answer.

    So its own transcript reaches it through the input instead. Ordering is the standing instruction
    first, then what it has already produced, then what it is being asked to change.

    Returns `input_dict` **unchanged and by identity** for every node that is not a redirect target
    with history of its own - which is every node in a DAG, every node on a forward pass, and every
    target that does have parents (those go through `contextual_input`).
    """
    if not run_ctx.is_redirect_target:
        return input_dict

    vectors = getattr(result_doc.result, "node_vectors", None) or {}
    vector = vectors.get(node)
    if vector is None:
        return input_dict
    slot = vector.slot(run_ctx.context_index)
    if not slot.entries:
        return input_dict

    start_node_id = run_ctx.start_node_id or None
    rendered = linearise_slot(slot, start_node_id=start_node_id)
    if rendered.is_empty:
        return input_dict

    sections: List[str] = []
    original = (input_dict or {}).get("text") or ""
    if original:
        sections.append(original)
    sections.append("## Your work so far")
    sections.append(rendered.text)

    message = message_for(
        getattr(result_doc.result, "edge_decisions", None),
        node,
        run_ctx.tag,
        context_index=run_ctx.context_index,
        turn_no=run_ctx.turn_no,
    )
    if message:
        sections.append(f"## Node {run_ctx.tag} sent this back to you")
        sections.append(message)

    urls = list((input_dict or {}).get("urls") or ())
    for url in rendered.urls:
        if url not in urls:
            urls.append(url)

    return {**(input_dict or {}), "text": "\n\n".join(sections), "urls": urls}


def contextual_input(
    result_doc: ActionGraphResultDoc,
    node: str,
    upstream_nodes: Iterable[str],
    *,
    run_ctx: NodeRunContext,
) -> Tuple[List[str], Dict[str, ActionDoc]]:
    """What to hand `to_input`: the parent list and the document map, composed for this context.

    Returns `(upstream_nodes, action_response)` **unchanged and by identity** whenever there is
    nothing to compose:

      - **K == 1** (`node_vectors is None`) - a DAG. The caller's own list and the live
        `node_results` map come back, so a DAG's prompt cannot differ from the one it composed
        before cyclic support existed.
      - **the baseline slot** - slot 0 mirrors `output[n]`, so a cyclic graph's forward pass
        composes exactly the prompt the identical DAG does.

    Otherwise, by role - see the module docstring.
    """
    upstream = list(upstream_nodes or ())
    responses = node_results(result_doc)

    vectors = getattr(result_doc.result, "node_vectors", None)
    context_index = run_ctx.context_index
    if not vectors or str(context_index) == BASELINE_SLOT:
        return upstream, responses

    start_node_id = run_ctx.start_node_id or None

    if run_ctx.is_redirect_target:
        return _target_input(
            result_doc, node, run_ctx=run_ctx, responses=responses, start_node_id=start_node_id
        )

    substituted = dict(responses)
    for parent in upstream:
        action_doc = responses.get(parent)
        if action_doc is None:
            continue
        vector: Optional[NodeContextVector] = vectors.get(parent)
        if vector is None:
            continue
        slot = vector.slot(context_index)
        if not slot.entries:
            # Contributing the value it already has (case (ii)) - which is what a node outside this
            # turn's R.S. legitimately does. Substituting a view built from an empty slot would
            # hand the model a blank where a real upstream value belongs.
            continue
        substituted[parent] = context_view(action_doc, slot, start_node_id=start_node_id)
    return upstream, substituted


def _target_input(
    result_doc: ActionGraphResultDoc,
    node: str,
    *,
    run_ctx: NodeRunContext,
    responses: Dict[str, ActionDoc],
    start_node_id: Optional[str],
) -> Tuple[List[str], Dict[str, ActionDoc]]:
    """The redirect target: its own slot, plus whatever the redirector handed back.

    Its data parents are dropped outright. They are upstream of the loop, they did not take part in
    this turn, and re-reading them is what left the node being told to try again reading a stale
    value and none of the feedback.
    """
    vectors = getattr(result_doc.result, "node_vectors", None) or {}
    own_doc = responses.get(node)
    own_vector = vectors.get(node)
    if own_doc is None or own_vector is None:
        # Never re-entered before, so there is nothing of its own to read. Fall back to the parent
        # list rather than composing an empty prompt.
        return list(run_ctx.data_parents), responses

    upstream: List[str] = [node]
    composed: Dict[str, ActionDoc] = dict(responses)
    composed[node] = context_view(
        own_doc,
        own_vector.slot(run_ctx.context_index),
        start_node_id=start_node_id,
        name=f"{own_doc.name or 'this node'} (your work so far)",
    )

    source = run_ctx.tag
    message = message_for(
        getattr(result_doc.result, "edge_decisions", None),
        node,
        source,
        context_index=run_ctx.context_index,
        turn_no=run_ctx.turn_no,
    )
    # Only when there is something to say. Nothing writes a message until Part 5, so today the
    # target reads its own transcript alone rather than an empty section attributed to the
    # redirector.
    if message and source and source in responses:
        composed[source] = _handback_view(responses[source], message)
        upstream.append(source)  # last, so the instruction is the most recent thing read

    return upstream, composed


__all__ = ["context_view", "contextual_input", "contextual_input_dict"]
