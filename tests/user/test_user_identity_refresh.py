"""The refresh runs on the request path, so the first test is the one that matters."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
import supabase_auth

import src.autobots.user.user as user_mod
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserDoc

USER_ID = "u1"


class _RecordingCRUD:


    calls: List[Dict[str, Any]] = []
    raise_on_write: bool = False

    def __init__(self, db: Any = None):
        pass

    async def set_identity_fields(self, user_id: str, fields: Dict[str, Any]) -> int:
        if _RecordingCRUD.raise_on_write:
            raise RuntimeError("mongo said no")
        _RecordingCRUD.calls.append({"user_id": user_id, "fields": fields})
        return 1


@pytest.fixture(autouse=True)
def crud(monkeypatch: pytest.MonkeyPatch):
    _RecordingCRUD.calls = []
    _RecordingCRUD.raise_on_write = False
    monkeypatch.setattr(user_mod, "UserCRUD", _RecordingCRUD)
    return _RecordingCRUD


def _stored(email: str = "a@example.com", metadata: Optional[Dict[str, Any]] = None) -> UserDoc:
    return UserDoc.model_construct(
        _id=USER_ID, id=USER_ID, user_id=USER_ID, email=email, user_metadata=metadata
    )


def _token(email: str = "a@example.com", metadata: Optional[Dict[str, Any]] = None):
    return supabase_auth.User(
        id=USER_ID, aud="authenticated", email=email, user_metadata=metadata or {},
        app_metadata={}, created_at="2026-01-01T00:00:00Z",
    )


async def _refresh(stored: UserDoc, token) -> UserDoc:
    return await user_mod.User._refresh_identity(
        ctx=Context(user_id=USER_ID), stored=stored, token_user=token, db=None
    )


@pytest.mark.asyncio
async def test_an_unchanged_identity_writes_nothing(crud):
    """Runs on every authenticated request."""
    metadata = {"full_name": "Anurag Jha"}
    out = await _refresh(_stored(metadata=metadata), _token(metadata=metadata))

    assert crud.calls == []
    assert out.user_metadata == metadata


@pytest.mark.asyncio
async def test_a_name_set_after_signup_reaches_the_row(crud):
    out = await _refresh(
        _stored(metadata={}), _token(metadata={"full_name": "Anurag Jha"})
    )

    assert crud.calls == [
        {"user_id": USER_ID, "fields": {"user_metadata": {"full_name": "Anurag Jha"}}}
    ]
    assert out.user_metadata == {"full_name": "Anurag Jha"}


@pytest.mark.asyncio
async def test_only_the_fields_that_differ_are_written(crud):
    out = await _refresh(
        _stored(email="old@example.com", metadata={"full_name": "Same"}),
        _token(email="new@example.com", metadata={"full_name": "Same"}),
    )

    assert list(crud.calls[0]["fields"]) == ["email"]
    assert out.email == "new@example.com"


@pytest.mark.asyncio
async def test_a_token_missing_metadata_does_not_erase_the_row(crud):
    stored = _stored(metadata={"full_name": "Anurag Jha"})

    out = await _refresh(stored, _token(metadata={}))

    assert crud.calls == []
    assert out.user_metadata == {"full_name": "Anurag Jha"}


@pytest.mark.asyncio
async def test_a_failed_write_does_not_fail_the_request(crud):
    crud.raise_on_write = True

    out = await _refresh(_stored(metadata={}), _token(metadata={"full_name": "Anurag Jha"}))

    assert out.user_metadata == {}, "returns what is stored, not what it hoped to store"


@pytest.mark.asyncio
async def test_a_thinner_token_does_not_clobber_a_richer_row(crud):
    """A password sign-in carries only `email_verified`; the Google one carries more."""
    stored = _stored(
        metadata={"email_verified": True, "full_name": "Anurag Jha", "avatar_url": "u"}
    )

    out = await _refresh(stored, _token(metadata={"email_verified": True}))

    assert crud.calls == []
    assert out.user_metadata["full_name"] == "Anurag Jha"


@pytest.mark.asyncio
async def test_the_merge_keeps_what_the_token_does_not_carry(crud):
    stored = _stored(metadata={"email_verified": True, "avatar_url": "keep-me"})

    out = await _refresh(
        stored, _token(metadata={"email_verified": True, "full_name": "Anurag Jha"})
    )

    written = crud.calls[0]["fields"]["user_metadata"]
    assert written == {
        "email_verified": True,
        "avatar_url": "keep-me",
        "full_name": "Anurag Jha",
    }
    assert out.user_metadata == written
