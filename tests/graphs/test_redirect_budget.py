"""Per-`(i, j)` redirect budgets - the guardrail that stops a cycle running forever.

The two properties that matter are both about *not* silently failing open: a DAG must carry no
budget field at all, and a counter must survive the retry layers rather than resetting under them.
"""

from __future__ import annotations

import pytest

from src.autobots.action_graph.action_graph.action_graph_doc_model import (
    ActionGraphDoc,
    ActionGraphUpdate,
    EdgeSpec,
)
from src.autobots.action_graph.action_graph.redirect_budget import (
    RedirectBudget,
    budget_key,
    exhausted_pairs,
    initial_budgets,
    may_fire,
    record_firing,
    split_key,
)


def an_edge(source: str, target: str) -> EdgeSpec:
    return EdgeSpec(source=source, target=target)


# --------------------------------------------------------------------------- keys


def test_a_key_round_trips():
    assert split_key(budget_key("0", "2")) == ("0", "2")


def test_the_key_reads_target_first_like_the_pair_notation():
    """The plan writes the pair `(i, j)` - destination first, source second."""
    assert budget_key("0", "2") == "0|2"


# --------------------------------------------------------------------------- the DAG case


def test_a_dag_gets_no_budget_map_at_all():
    """`None`, never `{}` - the same discipline `node_vectors` follows.

    A `{}` default would be written on every save and would make a DAG's run document differ from
    one produced before cyclic support existed.
    """
    assert initial_budgets([], default_limit=3) is None
    assert initial_budgets(None, default_limit=3) is None


def test_the_doc_model_defaults_to_none_on_every_shape():
    """`ActionGraphUpdate` especially: a non-None default is written on every partial save."""
    assert ActionGraphUpdate().redirect_budgets is None
    doc = ActionGraphDoc(_id="g", user_id="u", name="g", graph={})
    assert doc.redirect_budgets is None


# --------------------------------------------------------------------------- counting


def test_one_counter_per_edge_not_per_node():
    """Two edges into the same target are budgeted independently."""
    budgets = initial_budgets([an_edge("2", "0"), an_edge("5", "0")], default_limit=3)

    assert set(budgets) == {"0|2", "0|5"}
    assert all(b.fired == 0 and b.limit == 3 for b in budgets.values())


def test_firing_one_edge_leaves_the_other_untouched():
    """§1.9's point: a healthy cycle must not be starved by a busy one elsewhere."""
    budgets = initial_budgets([an_edge("2", "0"), an_edge("5", "0")], default_limit=3)

    budgets = record_firing(budgets, "0", "2", default_limit=3)
    budgets = record_firing(budgets, "0", "2", default_limit=3)

    assert budgets["0|2"].fired == 2
    assert budgets["0|5"].fired == 0


def test_record_firing_does_not_mutate_what_it_was_given():
    """A caller inspecting a budget must not accidentally advance it."""
    before = initial_budgets([an_edge("2", "0")], default_limit=3)

    after = record_firing(before, "0", "2", default_limit=3)

    assert before["0|2"].fired == 0, "the original map was mutated"
    assert after["0|2"].fired == 1
    assert after is not before


def test_an_unknown_edge_starts_counting_rather_than_being_ignored():
    """A graph whose edges changed between runs must still be bounded."""
    budgets = record_firing(None, "0", "9", default_limit=2)
    assert budgets["0|9"].fired == 1 and budgets["0|9"].limit == 2


# --------------------------------------------------------------------------- exhaustion


def test_an_edge_may_fire_until_it_hits_its_limit():
    budgets = initial_budgets([an_edge("2", "0")], default_limit=3)

    for expected in (True, True, True):
        assert may_fire(budgets, "0", "2") is expected
        budgets = record_firing(budgets, "0", "2", default_limit=3)

    assert may_fire(budgets, "0", "2") is False
    assert budgets["0|2"].fired == 3


def test_an_edge_with_no_counter_yet_may_fire():
    """Absence is 'not started', not 'denied'. Failing closed here would deadlock a fresh run."""
    assert may_fire(None, "0", "2") is True
    assert may_fire({}, "0", "2") is True


def test_exhaustion_is_derived_not_stored():
    """A stored flag could disagree with the counter - the reason `EdgeSpec` carries no `type`."""
    budgets = initial_budgets([an_edge("2", "0"), an_edge("5", "0")], default_limit=2)
    assert exhausted_pairs(budgets) == []

    budgets = record_firing(budgets, "0", "2", default_limit=2)
    budgets = record_firing(budgets, "0", "2", default_limit=2)

    assert exhausted_pairs(budgets) == [("0", "2")]
    assert budgets["0|2"].is_exhausted and not budgets["0|5"].is_exhausted


def test_a_zero_limit_disables_an_edge_outright():
    """Disabled, not exhausted.

    This test previously asserted the pair appeared in `exhausted_pairs` - encoding the very
    contradiction the field description warned against, since an exhausted pair parks the run.
    """
    budgets = initial_budgets([an_edge("2", "0")], default_limit=0)
    assert may_fire(budgets, "0", "2") is False
    assert budgets["0|2"].is_disabled
    assert exhausted_pairs(budgets) == [], "a disabled edge is nothing to ask a human about"


def test_raising_the_limit_re_arms_an_exhausted_pair():
    """D5's resume path: a human raises the limit and re-runs against the same result id."""
    budgets = initial_budgets([an_edge("2", "0")], default_limit=1)
    budgets = record_firing(budgets, "0", "2", default_limit=1)
    assert not may_fire(budgets, "0", "2")

    budgets["0|2"] = budgets["0|2"].model_copy(update={"limit": 3})

    assert may_fire(budgets, "0", "2")
    assert budgets["0|2"].fired == 1, "raising the limit does not erase the history"


# --------------------------------------------------------------------------- durability


def test_counters_survive_a_serialisation_round_trip():
    """RISK-4: the counter has to come back off the document after a retry, not restart at zero."""
    doc = ActionGraphDoc(
        _id="g",
        user_id="u",
        name="g",
        graph={},
        redirect_budgets=record_firing(
            initial_budgets([an_edge("2", "0")], default_limit=3), "0", "2", default_limit=3
        ),
    )

    revived = ActionGraphDoc.model_validate_json(doc.model_dump_json())

    assert revived.redirect_budgets["0|2"].fired == 1
    assert revived.redirect_budgets["0|2"].limit == 3
    assert isinstance(revived.redirect_budgets["0|2"], RedirectBudget)


@pytest.mark.parametrize("fired,limit,remaining", [(0, 3, 3), (2, 3, 1), (3, 3, 0), (5, 3, 0)])
def test_remaining_never_goes_negative(fired: int, limit: int, remaining: int):
    assert RedirectBudget(fired=fired, limit=limit).remaining == remaining


# --------------------------------------------------------------------------- disabled vs exhausted


def test_a_zero_limit_edge_is_disabled_not_exhausted():
    """The two states need different responses, so they must not collapse into one.

    `fired >= limit` is true from the start for a zero limit. Reported as *exhausted*, it reaches
    `exhausted_pairs_hit` and parks the run at `waiting` - asking a human to raise a limit they set
    to zero deliberately. The field has always documented 0 as "disables the edge entirely".
    """
    budgets = initial_budgets([an_edge("2", "0")], default_limit=0)

    assert budgets["0|2"].is_disabled
    assert not budgets["0|2"].is_exhausted, "a disabled edge never had a budget to run out of"
    assert exhausted_pairs(budgets) == [], "nothing to ask a human about"


def test_a_disabled_edge_may_not_fire():
    """`may_fire` gives the safe answer without the caller having to know about `is_disabled`."""
    budgets = initial_budgets([an_edge("2", "0")], default_limit=0)
    assert may_fire(budgets, "0", "2") is False


def test_an_exhausted_edge_is_still_exhausted():
    """The fix must not stop a real exhaustion from parking the run."""
    budgets = initial_budgets([an_edge("2", "0")], default_limit=1)
    budgets = record_firing(budgets, "0", "2", default_limit=1)

    assert budgets["0|2"].is_exhausted
    assert not budgets["0|2"].is_disabled
    assert may_fire(budgets, "0", "2") is False
    assert exhausted_pairs(budgets) == [("0", "2")]
