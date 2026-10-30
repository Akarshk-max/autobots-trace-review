"""Tests for the redirect / conditional edge fields on the action graph models.

The edge grammar is stored additively: `graph` keeps its existing meaning as the data-edge
adjacency, and non-data edges live in their own fields. A graph with neither field set is a plain
DAG and must behave exactly as it did before.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.autobots.action_graph.action_graph.action_graph_crud import ActionGraphCRUD
from src.autobots.action_graph.action_graph.action_graph_doc_model import (
    ActionGraphCreate,
    ActionGraphDoc,
    ActionGraphDocUpdate,
    ActionGraphLiteDoc,
    ActionGraphUpdate,
    EdgeSpec,
)


def a_graph_doc(**overrides: Any) -> ActionGraphDoc:
    payload: Dict[str, Any] = {
        "_id": "graph-1",
        "user_id": "user-1",
        "name": "g",
        "nodes": {"n1": "a1", "n2": "a2", "n3": "a3"},
        "graph": {"n1": ["n2"], "n2": ["n3"]},
    }
    payload.update(overrides)
    return ActionGraphDoc(**payload)


# CRUDBase.update_one runs the id through ObjectId(), so the CRUD tests need a real one.
OID = "507f1f77bcf86cd799439011"


def test_edge_spec_has_no_type_field():
    """The list an edge lives in types it; a `type` field would be a second source of truth.

    There is deliberately no EdgeType enum either - it would be the same duplication one level up.
    """
    assert "type" not in EdgeSpec.model_fields
    assert set(EdgeSpec.model_fields) == {"source", "target", "id"}


def test_edges_round_trip_through_the_doc():
    doc = a_graph_doc(
        redirect_edges=[{"source": "n3", "target": "n1"}],
        conditional_edges=[{"source": "n1", "target": "n2", "id": "e-7"}],
    )

    assert doc.redirect_edges == [EdgeSpec(source="n3", target="n1")]
    assert doc.conditional_edges[0].id == "e-7"

    reloaded = ActionGraphDoc.model_validate(doc.model_dump(by_alias=True))
    assert reloaded.redirect_edges == doc.redirect_edges
    assert reloaded.conditional_edges == doc.conditional_edges


def test_a_graph_stored_before_this_change_still_validates():
    """No migration: absent fields mean 'plain DAG'."""
    doc = a_graph_doc()

    assert doc.redirect_edges == []
    assert doc.conditional_edges == []


def test_create_defaults_to_empty_lists():
    created = ActionGraphCreate(name="g", graph={"n1": ["n2"]})

    assert created.redirect_edges == []
    assert created.conditional_edges == []


def test_update_defaults_to_none_not_empty_list():
    """`[]` here would mean 'clear the edges' on every unrelated save. See the next test."""
    update = ActionGraphUpdate(name="renamed")

    assert update.redirect_edges is None
    assert update.conditional_edges is None


@pytest.mark.asyncio
async def test_partial_update_does_not_wipe_stored_edges():
    """The contract that makes the None default load-bearing.

    `CRUDBase.update_one` builds its `$set` from `model_dump()` skipping only None, so anything
    with a non-None default is written on every save.
    """
    captured: Dict[str, Any] = {}

    async def fake_find_one_and_update(filter, update, return_document):
        captured["set"] = update["$set"]
        return {
            "_id": OID,
            "user_id": "user-1",
            "name": "renamed",
            "graph": {"n1": ["n2"]},
            "redirect_edges": [{"source": "n2", "target": "n1"}],
        }

    collection = MagicMock()
    collection.find_one_and_update = AsyncMock(side_effect=fake_find_one_and_update)
    db = MagicMock()
    db.__getitem__.return_value = collection

    crud = ActionGraphCRUD(db=db)
    rename_only = ActionGraphUpdate(name="renamed")
    result = await crud.update_one(
        ActionGraphDocUpdate(id=OID, user_id="user-1", **rename_only.model_dump())
    )

    assert isinstance(result, ActionGraphDoc), result
    assert "redirect_edges" not in captured["set"]
    assert "conditional_edges" not in captured["set"]
    assert captured["set"]["name"] == "renamed"
    # The stored edges survived the rename.
    assert result.redirect_edges == [EdgeSpec(source="n2", target="n1")]


@pytest.mark.asyncio
async def test_partial_update_does_not_reset_dashboard_settings():
    """These defaulted to 0.1 / 50 rather than None, so any save reset a user's configured values.

    `ActionUpdate` already gets this right for its equivalent pair; the graph model did not.
    """
    captured: Dict[str, Any] = {}

    async def fake_find_one_and_update(filter, update, return_document):
        captured["set"] = update["$set"]
        return {
            "_id": OID,
            "user_id": "user-1",
            "name": "renamed",
            "time_saved_per_file": 4.5,
            "dollar_cost_per_hour": 250,
        }

    collection = MagicMock()
    collection.find_one_and_update = AsyncMock(side_effect=fake_find_one_and_update)
    db = MagicMock()
    db.__getitem__.return_value = collection

    crud = ActionGraphCRUD(db=db)
    rename_only = ActionGraphUpdate(name="renamed")

    assert rename_only.time_saved_per_file is None
    assert rename_only.dollar_cost_per_hour is None

    result = await crud.update_one(
        ActionGraphDocUpdate(id=OID, user_id="user-1", **rename_only.model_dump())
    )

    assert "time_saved_per_file" not in captured["set"]
    assert "dollar_cost_per_hour" not in captured["set"]
    assert isinstance(result, ActionGraphDoc), result
    assert result.time_saved_per_file == 4.5
    assert result.dollar_cost_per_hour == 250


def test_create_still_supplies_the_dashboard_defaults():
    """Only the update model changed - a new graph must still get 0.1 / 50."""
    created = ActionGraphCreate(name="g")

    assert created.time_saved_per_file == 0.1
    assert created.dollar_cost_per_hour == 50


@pytest.mark.asyncio
async def test_update_can_still_set_edges_explicitly():
    """Omitting means 'leave alone'; passing a value - including [] - means 'write this'."""
    captured: Dict[str, Any] = {}

    async def fake_find_one_and_update(filter, update, return_document):
        captured["set"] = update["$set"]
        return {"_id": OID, "user_id": "user-1", "name": "g", "redirect_edges": []}

    collection = MagicMock()
    collection.find_one_and_update = AsyncMock(side_effect=fake_find_one_and_update)
    db = MagicMock()
    db.__getitem__.return_value = collection

    crud = ActionGraphCRUD(db=db)
    clear_them = ActionGraphUpdate(redirect_edges=[])
    await crud.update_one(
        ActionGraphDocUpdate(id=OID, user_id="user-1", **clear_them.model_dump())
    )

    assert captured["set"]["redirect_edges"] == []


def test_lite_doc_carries_no_topology():
    """ActionGraphLiteDoc is the list/search projection; edges are topology, like `graph`."""
    for field in ("graph", "nodes", "node_details", "redirect_edges", "conditional_edges"):
        assert field not in ActionGraphLiteDoc.model_fields


def test_a_run_freezes_the_edge_grammar_with_the_rest_of_the_graph():
    """`ActionGraphResultDoc.result` is an ActionGraphDoc, so a run keeps the topology it started
    with even if the workflow is edited afterwards."""
    from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
        ActionGraphResultDoc,
    )
    from src.autobots.event_result.event_result_model import EventResultStatus, EventType

    doc = a_graph_doc(redirect_edges=[{"source": "n3", "target": "n1"}])
    run = ActionGraphResultDoc(
        _id="run-1",
        user_id="user-1",
        type=EventType.action_graph_run,
        status=EventResultStatus.processing,
        result=doc,
    )

    frozen = ActionGraphResultDoc.model_validate(run.model_dump(by_alias=True))
    assert frozen.result.redirect_edges == [EdgeSpec(source="n3", target="n1")]


def test_deep_copy_share_preserves_edges():
    """UserShareData.clone_action_graph round-trips the doc through ActionGraphCreate.

    Node ids are not remapped by the clone (only action ids are), so edges stay valid.
    """
    source = a_graph_doc(
        redirect_edges=[{"source": "n3", "target": "n1"}],
        conditional_edges=[{"source": "n1", "target": "n2"}],
    )

    payload = source.model_dump(
        exclude={"id", "user_id", "created_at", "updated_at", "is_published", "shared_with"}
    )
    clone = ActionGraphCreate(**payload)

    assert clone.redirect_edges == source.redirect_edges
    assert clone.conditional_edges == source.conditional_edges
