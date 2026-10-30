"""The index-declaration mechanism, rather than any one index."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from pymongo import IndexModel

# Imported for their side effect: discovery only sees CRUDs that are loaded.
import src.autobots.action_graph.action_graph.action_graph_crud  # noqa: F401
import src.autobots.action_team.action_team.action_team_crud  # noqa: F401
import src.autobots.event_result.event_result_crud  # noqa: F401
import src.autobots.skills.skill_crud  # noqa: F401
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_indexes import declared_indexes, ensure_indexes


def test_every_declared_index_is_found():
    found = declared_indexes()
    for collection, name in (
        ("EventResults", "er_shared_with_idx"),
        ("ActionGraphs", "ag_shared_with_idx"),
        ("ActionTeams", "at_shared_with_idx"),
        ("Skills", "sk_shared_with_idx"),
    ):
        assert collection in found, f"{collection} declared nothing"
        assert name in [i.document["name"] for i in found[collection]]


def test_every_index_is_named():
    """An unnamed index gets a generated name, which is painful to drop later."""
    for collection, indexes in declared_indexes().items():
        for index in indexes:
            assert index.document.get("name"), f"unnamed index on {collection}"


def test_the_shared_grant_leads_the_index():
    """`shared_with` first: an index leading with `type` is the slow fallback."""
    er = declared_indexes()["EventResults"]
    idx = next(i for i in er if i.document["name"] == "er_shared_with_idx")
    assert list(idx.document["key"])[0] == "shared_with"


def test_the_collection_comes_from_the_doc_model():
    from src.autobots.event_result.event_result_model import EventResultDoc

    assert "EventResults" in declared_indexes()
    assert EventResultDoc.__collection__ == "EventResults"


def test_two_cruds_on_one_collection_declare_it_once():
    """Admin variants share a collection and declare the same indexes."""

    class _Doc:
        __collection__ = "SharedThing"

    shared = IndexModel([("a", 1)], name="only_once_idx")

    class _One(CRUDBase[_Doc, _Doc, _Doc, _Doc, _Doc, _Doc]):
        __indexes__ = [shared]

    class _Two(CRUDBase[_Doc, _Doc, _Doc, _Doc, _Doc, _Doc]):
        __indexes__ = [shared]

    try:
        names = [i.document["name"] for i in declared_indexes()["SharedThing"]]
        assert names == ["only_once_idx"], names
    finally:
        # These classes stay registered as subclasses for the rest of the session.
        for cls in (_One, _Two):
            cls.__indexes__ = []


@pytest.mark.asyncio
async def test_one_bad_collection_does_not_stop_the_rest():
    """A failing index must not keep the service from starting."""
    calls: List[str] = []

    class _Collection:
        def __init__(self, name: str):
            self._name = name

        async def create_indexes(self, indexes: List[IndexModel]) -> List[str]:
            calls.append(self._name)
            if self._name == "EventResults":
                raise RuntimeError("index build refused")
            return [i.document["name"] for i in indexes]

    class _Db:
        def __getitem__(self, name: str) -> Any:
            return _Collection(name)

    created: Dict[str, List[str]] = await ensure_indexes(_Db())

    assert "EventResults" in calls, "should have tried"
    assert "EventResults" not in created, "should not report a failure as created"
    assert "Skills" in created, "a later collection still ran"
