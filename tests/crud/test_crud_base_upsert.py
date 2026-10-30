"""`CRUDBase.upsert` — create-or-update, returning the doc either way.

Three call sites hand-rolled this before it existed (UserAccess, dashboard_cache,
UserPod), each slightly differently.
"""

from __future__ import annotations

import pytest
from pydantic import ConfigDict, Field

from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.updated_at import UpdatedAt


class _Doc(CreatedAt, UpdatedAt):
    __collection__ = "Fakes"
    id: str = Field(..., alias="_id")
    user_id: str
    label: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class _FakeCollection:
    def __init__(self):
        self.calls: list[tuple] = []
        self.name = "Fakes"

    async def find_one_and_update(self, filter, update, upsert=False, return_document=None):
        self.calls.append((filter, update, upsert, return_document))
        stored = {"_id": "doc-1"}
        stored.update(update.get("$setOnInsert", {}))
        stored.update(update.get("$set", {}))
        stored.setdefault("user_id", filter.get("user_id", "u1"))
        return stored


def _crud(doc_model=_Doc) -> tuple[CRUDBase, _FakeCollection]:
    collection = _FakeCollection()
    return CRUDBase(doc_model, doc_model, collection), collection


@pytest.mark.asyncio
async def test_the_match_is_passed_through_untouched():
    """Not via `_build_filter`, which injects `space_id: None` and would retarget
    the write to a different record."""
    crud, collection = _crud()
    await crud.upsert(match={"user_id": "u1", "space_id": "sp_1"}, set_fields={"label": "x"})

    query, _, upsert, return_document = collection.calls[0]
    assert query == {"user_id": "u1", "space_id": "sp_1"}
    assert upsert is True
    assert return_document is not None


@pytest.mark.asyncio
async def test_a_key_in_both_operators_is_rejected_by_name():
    """Mongo's own error for this does not say which field."""
    crud, _ = _crud()
    with pytest.raises(ValueError) as excinfo:
        await crud.upsert(
            match={"user_id": "u1"},
            set_fields={"label": "a"},
            set_on_insert={"label": "b"},
        )
    assert "label" in str(excinfo.value)


@pytest.mark.asyncio
async def test_nothing_is_written_that_the_caller_did_not_pass():
    """No model is built here, so CreatedAt/UpdatedAt cannot fire on their own."""
    crud, collection = _crud()
    await crud.upsert(match={"user_id": "u1"}, set_on_insert={"user_id": "u1"})

    _, update, _, _ = collection.calls[0]
    assert "$set" not in update
    assert update["$setOnInsert"] == {"user_id": "u1"}


@pytest.mark.asyncio
async def test_mixin_timestamps_are_written_as_given():
    """How callers supply them, so the stored value matches every other write."""
    crud, collection = _crud()
    stamps = UpdatedAt()
    await crud.upsert(match={"user_id": "u1"}, set_fields=stamps.model_dump())

    _, update, _, _ = collection.calls[0]
    assert update["$set"]["updated_at"] == stamps.updated_at


@pytest.mark.asyncio
async def test_the_stored_document_comes_back_parsed():
    crud, _ = _crud()
    doc = await crud.upsert(
        match={"user_id": "u1"}, set_fields={"label": "hello"}, set_on_insert={"user_id": "u1"}
    )

    assert isinstance(doc, _Doc)
    assert doc.id == "doc-1"
    assert doc.label == "hello"
