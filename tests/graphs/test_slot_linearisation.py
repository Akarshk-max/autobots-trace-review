"""Re-linearising a tagged transcript into prompt text - the read half of the context vector.

Parts 1-3.5 made a slot correct and left it write-only. `linearise_slot` is the first half of
making it readable: a pure function from a transcript to the text a prompt is built out of, so it
can be pinned on its own before anything is wired into a run path.

The properties worth protecting are the ones a plausible-looking implementation gets wrong:
turn numbers are printed rather than counted, ordering comes from the slot's own sort key, and an
unreadable cell costs one turn rather than the run.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.autobots.action_graph.action_graph.context_vector import (
    ContextEntry,
    ContextSlot,
    linearise_slot,
)

# Node ids are digit strings; the start node's is a letter. Same convention as the rest of the
# suite, and what makes the start-first rank in the sort key load-bearing.
START = "s"


def entry(
    node_id: str,
    turn_no: int,
    *,
    out: str = "",
    urls: Optional[List[str]] = None,
    inp: str = "",
    in_urls: Optional[List[str]] = None,
) -> ContextEntry:
    return ContextEntry(
        node_id=node_id,
        turn_no=turn_no,
        input={"text": inp, "urls": in_urls or []},
        output={"blocks": [{"text": out, "urls": urls or []}]},
    )


def headings(text: str) -> List[str]:
    return [line for line in text.splitlines() if line.startswith("### ")]


# --------------------------------------------------------------------------- the empty cases


def test_an_empty_slot_linearises_to_nothing():
    """A node with no transcript in this column must contribute no text, not an empty heading."""
    result = linearise_slot(ContextSlot(), start_node_id=START)

    assert result.text == ""
    assert result.urls == []
    assert result.is_empty


def test_a_slot_of_empty_outputs_still_reports_its_turns():
    """An action that produced no text still took a turn, and the transcript should say so."""
    slot = ContextSlot(entries=[entry(START, 0), entry("5", 0)])

    result = linearise_slot(slot, start_node_id=START)

    assert len(headings(result.text)) == 2
    assert not result.is_empty


# --------------------------------------------------------------------------- ordering and tags


def test_the_start_tag_leads_within_a_turn():
    """SS1.7's sort key, observed through the rendering rather than asserted on the slot.

    Node ids are digit strings and the start node's is a letter, so plain lexicographic ordering
    would sort the start node last - the opposite of what is wanted.
    """
    slot = ContextSlot(entries=[entry("5", 0, out="from 5"), entry(START, 0, out="from start")])

    text = linearise_slot(slot, start_node_id=START).text

    assert text.index("from start") < text.index("from 5")


def test_turns_are_rendered_in_ascending_order():
    slot = ContextSlot(
        entries=[entry("5", 2, out="third"), entry("5", 0, out="first"), entry("5", 1, out="second")]
    )

    text = linearise_slot(slot, start_node_id=START).text

    assert text.index("first") < text.index("second") < text.index("third")


def test_the_tag_says_which_kind_of_entry_it_is():
    """The tag namespace is provably disjoint (SS1.7.1), so a bare tag is unambiguous."""
    slot = ContextSlot(entries=[entry(START, 0), entry("5", 0)])

    text = linearise_slot(slot, start_node_id=START).text

    assert "initial pass" in text
    assert "redirected by node 5" in text


def test_without_a_start_node_id_nothing_claims_to_be_the_initial_pass():
    """`start_node_id` is Optional throughout the module; a malformed graph has none.

    Rendering every entry as "initial pass" would be worse than rendering none as one.
    """
    slot = ContextSlot(entries=[entry(START, 0), entry("5", 0)])

    text = linearise_slot(slot, start_node_id=None).text

    assert "initial pass" not in text
    assert f"redirected by node {START}" in text


# --------------------------------------------------------------------------- sparseness


def test_sparse_turn_numbers_are_printed_not_renumbered():
    """Turn numbers are NOT dense, and the rendering must not paper over a gap.

    A node only runs the turns whose `R.S.` contains it, so its recorded turns legitimately read
    [0, 2, 3]; an abandoned turn (RISK-12) leaves a hole too. Numbering the entries 1..N would
    silently renumber the gap away and tell the model a history it did not have.
    """
    slot = ContextSlot(entries=[entry("5", 0), entry("5", 2), entry("5", 3)])

    assert headings(linearise_slot(slot, start_node_id=START).text) == [
        "### Turn 0 - redirected by node 5",
        "### Turn 2 - redirected by node 5",
        "### Turn 3 - redirected by node 5",
    ]


def test_a_tag_that_never_reaches_turn_1_is_not_padded():
    """The start tag is pinned to turn 0 (SS1.7.1) - it is the only entry it will ever have.

    A transcript holding (s,0), (5,0), (5,1) must not grow a synthetic (s,1) to square the turns
    up. Missing combinations are simply absent.
    """
    slot = ContextSlot(entries=[entry(START, 0), entry("5", 0), entry("5", 1)])

    assert headings(linearise_slot(slot, start_node_id=START).text) == [
        "### Turn 0 - initial pass",
        "### Turn 0 - redirected by node 5",
        "### Turn 1 - redirected by node 5",
    ]


# --------------------------------------------------------------------------- payload


def test_output_text_is_carried_through():
    slot = ContextSlot(entries=[entry("5", 0, out="the reviewer disliked the tone")])

    assert "the reviewer disliked the tone" in linearise_slot(slot, start_node_id=START).text


def test_urls_are_collected_rather_than_folded_into_the_text():
    """A DataBlock carries urls beside its text and the engine attaches files from them.

    Rendering a url only into the prose would leave a re-entered node reading a filename it has no
    way to open.
    """
    slot = ContextSlot(entries=[entry("5", 0, out="see attached", urls=["s3://report.pdf"])])

    result = linearise_slot(slot, start_node_id=START)

    assert result.urls == ["s3://report.pdf"]


def test_a_url_referenced_by_several_turns_is_attached_once():
    """The attachment limit is counted in urls, so a file cited three times must not cost three."""
    slot = ContextSlot(
        entries=[
            entry("5", 0, urls=["s3://a.pdf"]),
            entry("5", 1, urls=["s3://a.pdf", "s3://b.pdf"]),
        ]
    )

    assert linearise_slot(slot, start_node_id=START).urls == ["s3://a.pdf", "s3://b.pdf"]


def test_input_is_left_out_by_default_and_included_on_request():
    """`include_input` is what DataFlowType.both_input_output asks for.

    Off by default: the input of a re-entry is largely the previous turn's output, so printing
    both doubles the prompt for little.
    """
    slot = ContextSlot(entries=[entry("5", 0, inp="please revise", out="revised")])

    without = linearise_slot(slot, start_node_id=START)
    with_input = linearise_slot(slot, start_node_id=START, include_input=True)

    assert "please revise" not in without.text
    assert "please revise" in with_input.text
    assert "revised" in with_input.text


def test_input_urls_are_collected_only_when_the_input_is_rendered():
    slot = ContextSlot(entries=[entry("5", 0, in_urls=["s3://brief.pdf"], urls=["s3://out.pdf"])])

    assert linearise_slot(slot, start_node_id=START).urls == ["s3://out.pdf"]
    assert linearise_slot(slot, start_node_id=START, include_input=True).urls == [
        "s3://brief.pdf",
        "s3://out.pdf",
    ]


# --------------------------------------------------------------------------- tolerance


def test_an_unreadable_cell_costs_its_turn_and_not_the_run():
    """Prompt composition sits in the middle of a run the user has already paid for.

    One entry whose payload will not validate should lose that turn of history, not raise into a
    run path whose only recourse is to fail the graph. Same posture as `to_input`, which wraps
    every upstream in `try`.
    """
    broken: Dict[str, Any] = {"blocks": "not a list of blocks"}
    slot = ContextSlot(
        entries=[
            entry("5", 0, out="readable"),
            ContextEntry(node_id="5", turn_no=1, output=broken),
            entry("5", 2, out="also readable"),
        ]
    )

    text = linearise_slot(slot, start_node_id=START).text

    assert "readable" in text and "also readable" in text
    assert "### Turn 1" not in text, "the unreadable turn should be skipped, not half-rendered"


def test_an_entry_with_no_output_at_all_is_still_rendered():
    """`ContextEntry.output` is Optional and a node can legitimately produce nothing."""
    slot = ContextSlot(entries=[ContextEntry(node_id="5", turn_no=0)])

    assert headings(linearise_slot(slot, start_node_id=START).text) == [
        "### Turn 0 - redirected by node 5"
    ]
