"""Tests for validation on the action-graph write path.

Validation is warn-only until `ACTION_GRAPH_VALIDATION_ENFORCED` is switched on: graphs already in
the collection predate any validation, so rejecting on write would make them uneditable.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.autobots.action_graph.action_graph.action_graph_doc_model import (
    ActionGraphCreate,
    ActionGraphDoc,
    ActionGraphUpdate,
    EdgeSpec,
)
from src.autobots.action_graph.action_graph.graph_topology import Severity, ValidationCode
from src.autobots.action_graph.action_graph.graph_validation import (
    describe_validation,
    merge_topology,
    touches_topology,
    validate_action_graph,
)
from src.autobots.data_model.context import Context

GOOD_GRAPH = {"s": ["a"], "a": ["b"]}
CYCLIC_GRAPH = {"s": ["a"], "a": ["b"], "b": ["a"]}


def a_doc(**overrides: Any) -> ActionGraphDoc:
    payload: Dict[str, Any] = {
        "_id": "graph-1",
        "user_id": "user-1",
        "name": "g",
        "graph": GOOD_GRAPH,
    }
    payload.update(overrides)
    return ActionGraphDoc(**payload)


# --------------------------------------------------------------------------- topology detection


def test_only_structural_updates_trigger_validation():
    """A rename cannot make a valid graph invalid, so it skips the extra read entirely."""
    assert not touches_topology(ActionGraphUpdate(name="renamed"))
    assert not touches_topology(ActionGraphUpdate(description="d", is_saved=True))

    assert touches_topology(ActionGraphUpdate(graph={"a": ["b"]}))
    assert touches_topology(ActionGraphUpdate(redirect_edges=[]))
    assert touches_topology(ActionGraphUpdate(conditional_edges=[]))
    assert touches_topology(ActionGraphUpdate(start_node_id="s"))
    assert touches_topology(ActionGraphUpdate(nodes={"a": "action-a"}))


def test_merge_keeps_nested_models_as_models():
    """`model_dump()` would turn redirect_edges into dicts, and `model_copy` does not re-validate."""
    merged = merge_topology(
        a_doc(),
        ActionGraphUpdate(redirect_edges=[EdgeSpec(source="b", target="a")]),
    )

    assert merged.redirect_edges == [EdgeSpec(source="b", target="a")]
    assert isinstance(merged.redirect_edges[0], EdgeSpec)


def test_merge_carries_the_new_name_for_diagnostics():
    """A rename saved alongside a restructure must not file the warning under the old name."""
    merged = merge_topology(
        a_doc(name="Old Name"),
        ActionGraphUpdate(name="New Name", graph=CYCLIC_GRAPH),
    )

    assert merged.name == "New Name"


def test_name_alone_still_skips_validation():
    """`name` is merged for the log line, but must not make a rename look structural."""
    assert not touches_topology(ActionGraphUpdate(name="renamed"))


def test_merge_ignores_fields_validation_never_reads():
    """The merged doc is a throwaway; only what validation reads needs to be current."""
    merged = merge_topology(
        a_doc(description="old desc"),
        ActionGraphUpdate(description="new desc", graph=CYCLIC_GRAPH),
    )

    assert merged.description == "old desc"


def test_merge_leaves_untouched_fields_alone():
    current = a_doc(redirect_edges=[{"source": "b", "target": "a"}])
    merged = merge_topology(current, ActionGraphUpdate(graph={"s": ["a"], "a": ["b"]}))

    assert merged.redirect_edges == current.redirect_edges


# --------------------------------------------------------------------------- warn vs enforce


def test_warn_only_reports_issues_without_raising(set_test_settings):
    issues = validate_action_graph(ctx=Context(), doc=a_doc(graph=CYCLIC_GRAPH))

    assert ValidationCode.data_edge_cycle in [i.code for i in issues]


def test_a_valid_graph_produces_no_issues(set_test_settings):
    assert validate_action_graph(ctx=Context(), doc=a_doc()) == []


def test_enforcing_turns_the_same_graph_into_a_400(monkeypatch, set_test_settings):
    from src.autobots import SettingsProvider

    settings = SettingsProvider.sget()
    monkeypatch.setattr(settings, "ACTION_GRAPH_VALIDATION_ENFORCED", True, raising=False)

    with pytest.raises(HTTPException) as exc:
        validate_action_graph(ctx=Context(), doc=a_doc(graph=CYCLIC_GRAPH))

    assert exc.value.status_code == 400
    assert exc.value.detail["issues"], "the 400 must say what is wrong"
    assert any(
        i["code"] == ValidationCode.data_edge_cycle.value for i in exc.value.detail["issues"]
    )


def test_validation_accepts_a_create_payload_that_has_no_id_yet(set_test_settings):
    """ActionGraphDoc subclasses ActionGraphCreate, so a graph is validatable before insert."""
    issues = validate_action_graph(
        ctx=Context(), doc=ActionGraphCreate(name="g", graph=CYCLIC_GRAPH)
    )

    assert ValidationCode.data_edge_cycle in [i.code for i in issues]


# --------------------------------------------------------------------------- severity


def test_half_built_graphs_produce_warnings_not_errors():
    """`create` fires on every new workflow and `update` on every autosave, so these are the
    common case - not failures. Reporting them as errors would bury the warn-only signal."""
    for label, kw in (
        ("brand new", {}),
        ("one unwired node", {"nodes": {"n1": "a1"}}),
    ):
        doc = ActionGraphDoc(_id="g", user_id="u", name=label)
        for k, v in kw.items():
            setattr(doc, k, v)
        issues = describe_validation(doc).issues
        assert issues, f"{label}: expected something to be reported"
        assert all(i.severity is Severity.warning for i in issues), label


def test_a_half_built_graph_is_still_valid():
    """Warnings describe an incomplete graph, not a malformed one."""
    report = describe_validation(ActionGraphDoc(_id="g", user_id="u", name="fresh"))

    assert report.is_valid is True
    assert [i.code for i in report.issues] == [ValidationCode.no_start_node]


def test_a_malformed_graph_is_an_error():
    report = describe_validation(a_doc(graph=CYCLIC_GRAPH))

    assert report.is_valid is False
    assert any(i.severity is Severity.error for i in report.issues)


def test_enforcing_does_not_reject_a_half_built_graph(monkeypatch, set_test_settings):
    """Enforcement must only ever act on the error class."""
    from src.autobots import SettingsProvider

    monkeypatch.setattr(
        SettingsProvider.sget(), "ACTION_GRAPH_VALIDATION_ENFORCED", True, raising=False
    )

    issues = validate_action_graph(ctx=Context(), doc=ActionGraphDoc(_id="g", user_id="u", name="fresh"))

    assert [i.severity for i in issues] == [Severity.warning]  # returned, but not raised


# --------------------------------------------------------------------------- service wiring


def _user_graphs(existing: ActionGraphDoc | None, captured: Dict[str, Any]):
    from src.autobots.action_graph.action_graph.user_action_graph import UserActionGraphs
    from src.autobots.user.user_orm_model import UserORM

    async def fake_find_one_and_update(filter, update, return_document):
        captured["set"] = update["$set"]
        return {"_id": "507f1f77bcf86cd799439011", "user_id": "user-1", "name": "g"}

    collection = MagicMock()
    collection.find_one_and_update = AsyncMock(side_effect=fake_find_one_and_update)
    db = MagicMock()
    db.__getitem__.return_value = collection

    graphs = UserActionGraphs(user=UserORM(id="user-1"), db=db)
    graphs.get_owned = AsyncMock(return_value=existing)
    return graphs


@pytest.mark.asyncio
async def test_update_validates_the_merged_result_not_the_patch(set_test_settings, monkeypatch):
    """The patch alone looks harmless; only merged with the stored graph is it invalid."""
    from src.autobots import SettingsProvider

    monkeypatch.setattr(
        SettingsProvider.sget(), "ACTION_GRAPH_VALIDATION_ENFORCED", True, raising=False
    )
    captured: Dict[str, Any] = {}
    graphs = _user_graphs(a_doc(), captured)

    # `b -> a` is only a cycle because the stored graph already has `a -> b`.
    with pytest.raises(HTTPException) as exc:
        await graphs.update(
            app_state=None,
            action_graph_id="507f1f77bcf86cd799439011",
            action_graph_update=ActionGraphUpdate(graph={"s": ["a"], "a": ["b"], "b": ["a"]}),
        )

    assert exc.value.status_code == 400
    assert "set" not in captured, "nothing should have been written"


@pytest.mark.asyncio
async def test_a_rename_skips_the_extra_read(set_test_settings):
    captured: Dict[str, Any] = {}
    graphs = _user_graphs(a_doc(), captured)

    await graphs.update(
        app_state=None,
        action_graph_id="507f1f77bcf86cd799439011",
        action_graph_update=ActionGraphUpdate(name="renamed"),
    )

    graphs.get_owned.assert_not_awaited()
    assert captured["set"]["name"] == "renamed"


@pytest.mark.asyncio
async def test_warn_only_update_still_writes(set_test_settings):
    captured: Dict[str, Any] = {}
    graphs = _user_graphs(a_doc(), captured)

    await graphs.update(
        app_state=None,
        action_graph_id="507f1f77bcf86cd799439011",
        action_graph_update=ActionGraphUpdate(graph=CYCLIC_GRAPH),
    )

    assert captured["set"]["graph"] == CYCLIC_GRAPH


# --------------------------------------------------------------------------- read-only report


def test_describe_validation_reports_a_clean_graph():
    report = describe_validation(a_doc())

    assert report.is_valid
    assert report.is_cyclic is False
    assert report.issues == []


def test_describe_validation_reports_problems_and_cyclicity():
    doc = a_doc(redirect_edges=[{"source": "b", "target": "s"}, {"source": "nope", "target": "s"}])
    report = describe_validation(doc)

    assert report.action_graph_id == "graph-1"
    assert report.is_cyclic
    assert ValidationCode.unknown_edge_endpoint in [i.code for i in report.issues]
