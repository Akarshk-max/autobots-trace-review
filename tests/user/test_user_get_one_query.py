"""`User.get` is on the auth path for every authenticated request."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

import src.autobots.user.user as user_mod
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM

UID = "u1"
ROW = {"_id": UID, "user_id": UID, "email": "a@example.com", "user_metadata": {}}


class _CountingCRUD:
    """Records which read methods were reached."""

    calls: List[str] = []
    row: Optional[Dict[str, Any]] = ROW

    def __init__(self, db: Any = None):
        pass

    async def find_one(self, doc_find: Any, or_find_queries: Any = None):
        _CountingCRUD.calls.append("find_one")
        return _CountingCRUD.row

    async def find_page(self, doc_find: Any, **kwargs: Any):
        _CountingCRUD.calls.append("find_page")
        raise AssertionError("find_page runs a count as well; not on this path")

    async def count(self, find_params: Any):
        _CountingCRUD.calls.append("count")
        return 1


@pytest.fixture(autouse=True)
def crud(monkeypatch: pytest.MonkeyPatch):
    _CountingCRUD.calls = []
    _CountingCRUD.row = dict(ROW)
    monkeypatch.setattr(user_mod, "UserCRUD", _CountingCRUD)
    return _CountingCRUD


@pytest.mark.asyncio
async def test_the_lookup_is_a_single_query(crud):
    doc = await user_mod.User.get(
        ctx=Context(user_id=UID), user=UserORM(id=UID), id=UID, db=None
    )

    assert crud.calls == ["find_one"]
    assert doc.user_id == UID


@pytest.mark.asyncio
async def test_a_missing_user_is_still_a_404(crud):
    from fastapi import HTTPException

    crud.row = None

    with pytest.raises(HTTPException) as caught:
        await user_mod.User.get(
            ctx=Context(user_id=UID), user=UserORM(id=UID), id=UID, db=None
        )

    assert caught.value.status_code == 404
    assert crud.calls == ["find_one"]
