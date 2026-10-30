from __future__ import annotations

import json
from io import BytesIO
from typing import Any, Dict, Optional

import pytest
from fastapi import HTTPException

import src.autobots.action.action_result.user_action_result as uar_mod
from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action_result.action_result_doc_model import (
    ActionResultCreate,
    ActionResultUpdate,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import (
    EventResultDoc,
    EventResultDocCreate,
    EventResultDocFind,
    EventResultDocUpdate,
    EventResultStatus,
    EventType,
)
from src.autobots.files.user_files_model import FileMeta
from src.autobots.user.user_orm_model import UserORM


class FakeUserFiles:
    """In-memory stand-in for UserFiles backed by a dict."""

    # set per-test
    get_bytes_calls: int = 0
    raise_on_get_bytes: bool = False
    _store: Dict[str, bytes] = {}
    fail_upload: bool = False

    def __init__(
        self,
        user: UserORM,
        s3: Any = None,  # noqa: ANN401 - test fake
        settings: Any = None,  # noqa: ANN401 - test fake
        top_level_folder: str = "",
    ):
        self._user = user
        self._prefix = f"{top_level_folder}/{str(user.id)}/" if top_level_folder else ""
    async def upload_binary(self, ctx: Context, file_obj: BytesIO, filename: str):
        if FakeUserFiles.fail_upload:
            raise RuntimeError("s3 upload failed")
        file_obj.seek(0)
        data = file_obj.read()
        key = f"{self._prefix}{filename}".replace("//", "/")
        FakeUserFiles._store[key] = data
        return [FileMeta(name=filename, url=f"https://test-bucket.s3.amazonaws.com/{key}")]

    async def get_bytes(self, ctx: Context, filename: str) -> Optional[bytes]:
        FakeUserFiles.get_bytes_calls += 1
        if FakeUserFiles.raise_on_get_bytes:
            raise RuntimeError("unexpected s3 get_bytes")
        key = f"{self._prefix}{filename}".replace("//", "/")
        return FakeUserFiles._store.get(key)

    async def delete_files(self, ctx: Context, filenames: list[str]) -> list[str]:
        deleted: list[str] = []
        for filename in filenames:
            key = f"{self._prefix}{filename}".replace("//", "/")
            if key in FakeUserFiles._store:
                del FakeUserFiles._store[key]
                deleted.append(filename)
        return deleted


class FakeDeleteResult:
    def __init__(self, deleted_count: int):
        self.deleted_count = deleted_count



class FakeEventResultsCollection:
    def __init__(self, crud: "FakeEventResultCRUD"):
        self._crud = crud
        self.fail_update = False
        self.return_none = False

    async def find_one_and_update(self, filter: dict, update: dict, return_document=None):  # noqa: ANN001
        if self.fail_update:
            raise RuntimeError("mongo update failed")
        if self.return_none:
            return None
        doc_id = str(filter.get("_id"))
        existing = self._crud._docs.get(doc_id)
        if existing is None:
            return None
        set_fields = update.get("$set", {})
        updated = existing.model_copy(update=set_fields)
        self._crud._docs[doc_id] = updated
        return updated.model_dump(by_alias=True)

    async def delete_one(self, filter: dict):  # noqa: ANN001
        doc_id = str(filter.get("_id"))
        self._crud._docs.pop(doc_id, None)
        return None

class FakeEventResultCRUD:
    """In-memory stand-in for EventResultCRUD."""

    def __init__(self, db: Any = None):  # noqa: ANN401 - test fake
        self._docs: Dict[str, EventResultDoc] = {}
        self._counter = 0
        self.document = FakeEventResultsCollection(self)
    @property
    def docs(self) -> Dict[str, EventResultDoc]:
        return self._docs

    async def insert_one(self, doc_create: EventResultDocCreate) -> EventResultDoc:
        self._counter += 1
        _id = f"ar_{self._counter}"
        doc = EventResultDoc.model_validate(
            {**doc_create.model_dump(), "_id": _id, "file_url": None}
        )
        self._docs[_id] = doc
        return doc

    async def update_one(self, doc_update: EventResultDocUpdate) -> EventResultDoc:
        data = doc_update.model_dump(exclude_none=True)
        _id = data.get("id")
        assert _id, "update_one requires id"
        existing = self._docs[_id]

        updated = existing.model_copy(update=data)
        self._docs[_id] = updated
        return updated

    async def find_one(
        self,
        doc_find: EventResultDocFind,
        or_find_queries: Optional[list] = None,
    ) -> Optional[EventResultDoc]:
        doc = self._docs.get(doc_find.id)
        if doc is None:
            return None
        # Owner-scoped lookups have to miss for everybody else, or a test cannot
        # tell the owned path from the shared one - which is the whole
        # distinction the sharing code turns on.
        if doc_find.user_id and doc.user_id != doc_find.user_id:
            return None
        if doc_find.shared_with and doc_find.shared_with not in (doc.shared_with or []):
            return None
        return doc

    async def delete_many(self, doc_find: EventResultDocFind) -> FakeDeleteResult:
        if doc_find.id in self._docs:
            del self._docs[doc_find.id]
            return FakeDeleteResult(deleted_count=1)
        return FakeDeleteResult(deleted_count=0)
@pytest.fixture()
def fake_stores(monkeypatch: pytest.MonkeyPatch):
    # Reset in-memory stores per test
    FakeUserFiles._store = {}
    FakeUserFiles.fail_upload = False
    FakeUserFiles.get_bytes_calls = 0
    FakeUserFiles.raise_on_get_bytes = False

    # Patch the module-level deps used by UserActionResult.__init__
    monkeypatch.setattr(uar_mod, "UserFiles", FakeUserFiles)
    monkeypatch.setattr(uar_mod, "EventResultCRUD", FakeEventResultCRUD)
    monkeypatch.setattr(uar_mod, "get_s3", lambda: None)

    user = UserORM(id="u1", email="u1@example.com")
    uar = uar_mod.UserActionResult(user=user, db=None)
    assert isinstance(uar.event_result_crud, FakeEventResultCRUD)
    assert isinstance(uar.fs_crud, FakeUserFiles)

    # Reset Mongo update failure flags
    uar.event_result_crud.document.fail_update = False
    uar.event_result_crud.document.return_none = False

    return uar, FakeUserFiles._store, uar.event_result_crud.docs

def _make_action_doc(*, user_id: str, action_id: str = "a1", output: dict | None = None) -> ActionDoc:
    return ActionDoc(
        id=action_id,
        user_id=user_id,
        name="My Action",
        type=ActionType.openai_responses,
        config={"k": "v"},
        input={"hello": "world"},
        output=output or {"ok": True},
    )


@pytest.mark.asyncio
async def test_action_result_full_in_s3_lite_in_mongo(fake_stores):
    uar, s3_store, mongo_store = fake_stores
    ctx = Context(user_id=uar.user_id)

    action_doc = _make_action_doc(user_id=uar.user_id, output={"answer": 42})
    action_result = ActionResultCreate(
        name="run",
        type=EventType.action_run,
        status=EventResultStatus.success,
        result=action_doc,
    )

    created = await uar.create_action_result(action_result=action_result, ctx=ctx)
    assert created is not None
    assert created.id

    # Mongo: lite version (no config/input/output in stored result dict)
    mongo_doc = mongo_store[created.id]
    assert isinstance(mongo_doc.result, dict)
    assert "config" not in mongo_doc.result
    assert "input" not in mongo_doc.result
    assert "output" not in mongo_doc.result
    assert mongo_doc.file_url  # pointer should be set

    # S3: full version (includes output)
    key = f"action_runs/{uar.user_id}/{created.id}.json"
    assert key in s3_store
    payload = json.loads(s3_store[key].decode("utf-8"))
    assert payload["result"]["output"] == {"answer": 42}


@pytest.mark.asyncio
async def test_action_result_update_writes_s3_and_mongo(fake_stores):
    uar, s3_store, mongo_store = fake_stores
    ctx = Context(user_id=uar.user_id)

    action_doc = _make_action_doc(user_id=uar.user_id, output={"v": 1})
    action_result = ActionResultCreate(
        name="run",
        type=EventType.action_run,
        status=EventResultStatus.success,
        result=action_doc,
    )
    created = await uar.create_action_result(action_result=action_result, ctx=ctx)

    update = ActionResultUpdate(
        status=EventResultStatus.error,
        result=_make_action_doc(user_id=uar.user_id, action_id="a1", output={"v": 2}),
    )
    updated = await uar.update_action_result(created.id, update, ctx=ctx)

    # Mongo updated
    mongo_doc = mongo_store[created.id]
    assert mongo_doc.status == EventResultStatus.error
    assert mongo_doc.file_url
    assert "output" not in mongo_doc.result  # still lite

    # S3 updated
    key = f"action_runs/{uar.user_id}/{created.id}.json"
    payload = json.loads(s3_store[key].decode("utf-8"))
    assert payload["status"] == EventResultStatus.error
    assert payload["result"]["output"] == {"v": 2}

    # Returned value is full
    assert updated.status == EventResultStatus.error
    assert updated.result.output == {"v": 2}


@pytest.mark.asyncio
async def test_action_result_missing_s3_payload_raises_404(fake_stores):
    uar, s3_store, mongo_store = fake_stores
    ctx = Context(user_id=uar.user_id)

    action_doc = _make_action_doc(user_id=uar.user_id)
    action_result = ActionResultCreate(
        name="run",
        type=EventType.action_run,
        status=EventResultStatus.success,
        result=action_doc,
    )
    created = await uar.create_action_result(action_result=action_result, ctx=ctx)

    # Simulate inconsistent state: Mongo has pointer, but S3 payload missing
    key = f"action_runs/{uar.user_id}/{created.id}.json"
    del s3_store[key]

    with pytest.raises(HTTPException) as exc:
        await uar.get_action_result(action_result_id=created.id, ctx=ctx)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_action_result_legacy_fallback_returns_mongo_full(fake_stores):
    uar, s3_store, mongo_store = fake_stores
    ctx = Context(user_id=uar.user_id)

    # Insert a legacy doc where Mongo contains the full payload and file_url is empty
    # Ensure legacy read path does NOT touch S3 at all
    FakeUserFiles.raise_on_get_bytes = True
    action_doc = _make_action_doc(user_id=uar.user_id, output={"legacy": True})
    legacy_id = "legacy_1"

    legacy_doc_create = EventResultDocCreate(
        user_id=uar.user_id,
        name="legacy",
        type=EventType.action_run,
        status=EventResultStatus.success,
        result=action_doc.model_dump(),
    )
    legacy_doc = EventResultDoc.model_validate(
        {**legacy_doc_create.model_dump(), "_id": legacy_id, "file_url": ""}
    )
    # put into fake mongo
    uar.event_result_crud.docs[legacy_id] = legacy_doc

    # S3 has no object, but legacy should still return from mongo
    got = await uar.get_action_result(action_result_id=legacy_id, ctx=ctx)
    assert got is not None
    assert got.id == legacy_id
    assert got.result.output == {"legacy": True}


@pytest.mark.asyncio
async def test_create_rolls_back_mongo_on_s3_upload_failure(fake_stores):
    uar, s3_store, mongo_store = fake_stores
    ctx = Context(user_id=uar.user_id)
    assert FakeUserFiles.get_bytes_calls == 0

    FakeUserFiles.fail_upload = True

    action_doc = _make_action_doc(user_id=uar.user_id)
    action_result = ActionResultCreate(
        name="run",
        type=EventType.action_run,
        status=EventResultStatus.success,
        result=action_doc,
    )

    with pytest.raises(RuntimeError):
        await uar.create_action_result(action_result=action_result, ctx=ctx)

    # Mongo insert should have been rolled back
    assert mongo_store == {}
    assert s3_store == {}


@pytest.mark.asyncio
async def test_create_rolls_back_both_on_mongo_file_url_update_failure(fake_stores):
    uar, s3_store, mongo_store = fake_stores
    ctx = Context(user_id=uar.user_id)

    # Fail the Mongo pointer update step
    uar.event_result_crud.document.fail_update = True

    action_doc = _make_action_doc(user_id=uar.user_id)
    action_result = ActionResultCreate(
        name="run",
        type=EventType.action_run,
        status=EventResultStatus.success,
        result=action_doc,
    )

    with pytest.raises(RuntimeError):
        await uar.create_action_result(action_result=action_result, ctx=ctx)

    # Both S3 and Mongo should have been rolled back
    assert mongo_store == {}
    assert s3_store == {}


@pytest.mark.asyncio
async def test_update_rolls_back_s3_when_mongo_update_fails(fake_stores):
    uar, s3_store, mongo_store = fake_stores
    ctx = Context(user_id=uar.user_id)

    action_doc = _make_action_doc(user_id=uar.user_id, output={"v": 1})
    action_result = ActionResultCreate(
        name="run",
        type=EventType.action_run,
        status=EventResultStatus.success,
        result=action_doc,
    )
    created = await uar.create_action_result(action_result=action_result, ctx=ctx)

    key = f"action_runs/{uar.user_id}/{created.id}.json"
    before_bytes = s3_store[key]

    # Fail Mongo update after S3 write
    uar.event_result_crud.document.fail_update = True

    update = ActionResultUpdate(
        status=EventResultStatus.error,
        result=_make_action_doc(user_id=uar.user_id, output={"v": 2}),
    )

    with pytest.raises(RuntimeError):
        await uar.update_action_result(created.id, update, ctx=ctx)

    # S3 should be rolled back to previous bytes
    assert s3_store[key] == before_bytes


@pytest.mark.asyncio
async def test_update_legacy_rolls_back_new_s3_object_when_mongo_update_fails(fake_stores):
    uar, s3_store, mongo_store = fake_stores
    ctx = Context(user_id=uar.user_id)

    # Ensure legacy update path does NOT prefetch S3 bytes (no file_url yet)
    FakeUserFiles.raise_on_get_bytes = True
    # Insert a legacy doc where Mongo contains the full payload and file_url is empty
    action_doc = _make_action_doc(user_id=uar.user_id, output={"legacy": True})
    legacy_id = "legacy_1"

    legacy_doc_create = EventResultDocCreate(
        user_id=uar.user_id,
        name="legacy",
        type=EventType.action_run,
        status=EventResultStatus.success,
        result=action_doc.model_dump(),
    )
    legacy_doc = EventResultDoc.model_validate(
        {**legacy_doc_create.model_dump(), "_id": legacy_id, "file_url": ""}
    )
    uar.event_result_crud.docs[legacy_id] = legacy_doc

    assert s3_store == {}

    uar.event_result_crud.document.fail_update = True

    update = ActionResultUpdate(
        status=EventResultStatus.error,
        result=_make_action_doc(user_id=uar.user_id, output={"legacy": False}),
    )

    with pytest.raises(RuntimeError):
        await uar.update_action_result(legacy_id, update, ctx=ctx)

    # No new S3 object should remain after rollback
    key = f"action_runs/{uar.user_id}/{legacy_id}.json"
    assert key not in s3_store
    assert FakeUserFiles.get_bytes_calls == 0

    # Legacy mongo doc should still be present
    assert legacy_id in mongo_store


@pytest.mark.asyncio
async def test_a_grantee_gets_the_turns_not_just_the_lite_doc(fake_stores, monkeypatch):
    """The conversation lives in S3, under the owner's prefix.

    Mongo keeps an `ActionLiteDoc`, which has no `results`. Returning that to a
    grantee gave them a run with no turns in it — the UI rendered "no
    conversation data" on a run that plainly had a conversation. And the payload
    cannot be read with the caller's own `UserFiles`, because it prefixes every
    key with the id of the user it was built for.
    """
    owner, s3_store, mongo_store = fake_stores
    ctx = Context(user_id=owner.user_id)

    action_doc = _make_action_doc(user_id=owner.user_id, output={"answer": 42})
    created = await owner.create_action_result(
        action_result=ActionResultCreate(
            name="run",
            type=EventType.action_run,
            status=EventResultStatus.success,
            result=action_doc,
        ),
        ctx=ctx,
    )
    assert created is not None

    # Mongo holds the lite copy: this is what the grantee used to receive.
    assert "output" not in mongo_store[created.id].result

    mongo_store[created.id] = mongo_store[created.id].model_copy(
        update={"shared_with": ["u2@example.com"]}
    )

    grantee = uar_mod.UserActionResult(
        user=UserORM(id="u2", email="u2@example.com"), db=None
    )
    # One database, two callers. Each fake CRUD keeps its own dict, so without
    # this the grantee would read an empty store and "pass" for the wrong reason.
    grantee.event_result_crud = owner.event_result_crud

    shared = await grantee.get_owned_or_shared_action_result(created.id, ctx)

    assert shared is not None
    # The full ActionDoc, read from u1's prefix rather than u2's.
    assert shared.result.output == {"answer": 42}
    assert shared.result.config == {"k": "v"}


@pytest.mark.asyncio
async def test_a_stranger_still_gets_nothing(fake_stores):
    """The hydration must not become a way around the grant list."""
    owner, _s3, mongo_store = fake_stores
    ctx = Context(user_id=owner.user_id)

    created = await owner.create_action_result(
        action_result=ActionResultCreate(
            name="run",
            type=EventType.action_run,
            status=EventResultStatus.success,
            result=_make_action_doc(user_id=owner.user_id),
        ),
        ctx=ctx,
    )
    assert created is not None

    stranger = uar_mod.UserActionResult(
        user=UserORM(id="u3", email="u3@example.com"), db=None
    )
    stranger.event_result_crud = owner.event_result_crud

    assert await stranger.get_owned_or_shared_action_result(created.id, ctx) is None
