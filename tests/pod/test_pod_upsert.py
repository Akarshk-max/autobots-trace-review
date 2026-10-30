"""The pod upsert: what it sends to Mongo, and what it leaves alone.

A pod is one record per user, created on first read. These drive a real `PodCRUD` over
a fake collection, so they cover `CRUDBase.upsert` as well.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.autobots.pod.pod_doc_model import PodDoc, PodUpdate
from src.autobots.pod.user_pod import UserPod

USER_ID = "user-1"


class _FakeCollection:
    """Records the write and echoes back what Mongo would have stored."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.name = "Pods"  # CRUDBase derives its index names from this

    async def find_one_and_update(self, filter, update, upsert=False, return_document=None):
        self.calls.append((filter, update, upsert, return_document))
        stored = {"_id": "pod-1"}
        stored.update(update.get("$setOnInsert", {}))
        stored.update(update.get("$set", {}))
        return stored


class _FakeDb:
    def __init__(self, collection):
        self._collection = collection

    def __getitem__(self, _name):
        return self._collection


def _user_pod(space_member=None) -> tuple[UserPod, _FakeCollection]:
    """A real UserPod over a real PodCRUD over a fake collection.

    Going through the real CRUD means these also cover `CRUDBase.upsert`.
    """
    collection = _FakeCollection()
    user = SimpleNamespace(id=USER_ID, space_member=space_member)
    return UserPod(user, _FakeDb(collection)), collection


# --- what goes to Mongo --------------------------------------------------------


@pytest.mark.asyncio
async def test_the_pod_is_keyed_by_user_and_space():
    pod, collection = _user_pod()
    await pod.get(None)

    query, _, upsert, _ = collection.calls[0]
    assert query == {"user_id": USER_ID, "space_id": None}
    assert upsert is True


@pytest.mark.asyncio
async def test_no_key_appears_in_both_operators():
    """Mongo rejects a field present in `$set` and `$setOnInsert` at once."""
    pod, collection = _user_pod()
    await pod.update(None, PodUpdate(current_runnable_id="a1", current_run_id="r1"))

    _, update, _, _ = collection.calls[0]
    assert not set(update["$set"]) & set(update["$setOnInsert"])


@pytest.mark.asyncio
async def test_identity_and_created_at_are_only_written_on_insert():
    """Otherwise every read would rewrite created_at."""
    pod, collection = _user_pod()
    await pod.get(None)

    _, update, _, _ = collection.calls[0]
    assert set(update["$setOnInsert"]) == {"user_id", "space_id", "created_at"}
    assert "updated_at" in update["$set"]


@pytest.mark.asyncio
async def test_reading_a_pod_writes_no_fields_of_its_own():
    """A read creates the record but must not blank anything on an existing one."""
    pod, collection = _user_pod()
    await pod.get(None)

    _, update, _, _ = collection.calls[0]
    assert set(update["$set"]) == {"updated_at"}


# --- partial update ------------------------------------------------------------


@pytest.mark.asyncio
async def test_updating_one_field_leaves_the_other_alone():
    """Sending only the run id must not blank the runnable id."""
    pod, collection = _user_pod()
    await pod.update(None, PodUpdate(current_run_id="run-9"))

    _, update, _, _ = collection.calls[0]
    assert update["$set"]["current_run_id"] == "run-9"
    assert "current_runnable_id" not in update["$set"]


@pytest.mark.asyncio
async def test_the_updated_pod_comes_back():
    pod, collection = _user_pod()
    doc = await pod.update(None, PodUpdate(current_runnable_id="agent-7"))

    assert isinstance(doc, PodDoc)
    assert doc.current_runnable_id == "agent-7"
    assert doc.user_id == USER_ID
    assert doc.id == "pod-1"

    _, _, _, return_document = collection.calls[0]
    assert return_document is not None, "must return the document after the write"


# --- space scoping -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_space_member_gets_a_pod_scoped_to_that_space():
    pod, collection = _user_pod(space_member=SimpleNamespace(space_id="sp_1"))
    await pod.get(None)

    query, update, _, _ = collection.calls[0]
    assert query["space_id"] == "sp_1"
    assert update["$setOnInsert"]["space_id"] == "sp_1"


@pytest.mark.asyncio
async def test_a_space_member_without_a_space_id_is_rejected():
    from fastapi import HTTPException

    pod, _ = _user_pod(space_member=SimpleNamespace(space_id=None))
    with pytest.raises(HTTPException):
        await pod.get(None)


# --- model shape ---------------------------------------------------------------


def test_the_collection_is_pods():
    assert PodDoc.__collection__ == "Pods"


def test_an_update_with_nothing_set_writes_nothing():
    assert PodUpdate().model_dump(exclude_none=True) == {}
