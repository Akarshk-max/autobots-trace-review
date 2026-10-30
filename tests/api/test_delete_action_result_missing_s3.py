from __future__ import annotations

import sys
import types

import pytest
from fastapi import HTTPException


def _ensure_fake_supabase_module() -> None:
    """Some test environments don't have the `supabase` package installed.

    The API module imports auth security code which imports our supabase connector. We stub
    the external `supabase` module so importing the API router module doesn't fail.
    """
    if "supabase" in sys.modules:
        return
    fake = types.ModuleType("supabase")
    class _Client:  # noqa: WPS431
        pass
    def create_client(*args, **kwargs):  # noqa: ANN001
        return _Client()
    fake.Client = _Client
    fake.create_client = create_client
    sys.modules["supabase"] = fake

from src.autobots.action.action.action_doc_model import ActionDoc  # noqa: E402
from src.autobots.action.action_result.action_result_doc_model import ActionResultDoc  # noqa: E402
from src.autobots.action.action_type.action_types import ActionType  # noqa: E402
from src.autobots.event_result.event_result_model import EventResultStatus, EventType  # noqa: E402
from src.autobots.user.user_orm_model import UserORM  # noqa: E402


def _make_action_result_doc(*, run_id: str, user_id: str) -> ActionResultDoc:
    action_doc = ActionDoc(
        id="a1",
        user_id=user_id,
        name="My Action",
        type=ActionType.openai_responses,
        config={"k": "v"},
    )
    return ActionResultDoc(
        id=run_id,
        user_id=user_id,
        type=EventType.action_run,
        status=EventResultStatus.success,
        result=action_doc,
        file_url="",
    )


@pytest.mark.asyncio
async def test_delete_action_result_allows_delete_when_s3_payload_missing(monkeypatch):
    _ensure_fake_supabase_module()
    from src.autobots.api.v1.api_action_result import api_action_results as api_mod

    run_id = "r1"
    user_id = "u1"

    class FakeUserActionResult:
        def __init__(self, user: UserORM, db):  # noqa: ANN001
            self.user = user
            self.db = db

        async def get_action_result(self, _id: str):
            raise HTTPException(status_code=404, detail="Action result payload missing in S3")

        async def get_action_result_mongo(self, action_result_id: str, ctx=None):  # noqa: ANN001
            assert action_result_id == run_id
            return _make_action_result_doc(run_id=run_id, user_id=user_id)

        async def delete_action_result(self, _id: str, ctx=None):  # noqa: ANN001
            assert _id == run_id
            return 1

    monkeypatch.setattr(api_mod, "UserActionResult", FakeUserActionResult)

    user_res = types.SimpleNamespace(user=types.SimpleNamespace(id=user_id))
    fake_request = types.SimpleNamespace(state=types.SimpleNamespace(context=None))

    deleted_doc = await api_mod.delete_action_result(
        request=fake_request, id=run_id, user_res=user_res, db=None,
        space_member=None,
    )
    assert deleted_doc.id == run_id


@pytest.mark.asyncio
async def test_delete_action_result_404_when_missing_everywhere(monkeypatch):
    _ensure_fake_supabase_module()
    from src.autobots.api.v1.api_action_result import api_action_results as api_mod

    run_id = "r_missing"
    user_id = "u1"

    class FakeUserActionResult:
        def __init__(self, user: UserORM, db):  # noqa: ANN001
            self.user = user
            self.db = db

        async def get_action_result(self, _id: str):
            raise HTTPException(status_code=404, detail="Action result payload missing in S3")

        async def get_action_result_mongo(self, action_result_id: str, ctx=None):  # noqa: ANN001
            return None

        async def delete_action_result(self, _id: str, ctx=None):  # noqa: ANN001
            return 0

    monkeypatch.setattr(api_mod, "UserActionResult", FakeUserActionResult)

    user_res = types.SimpleNamespace(user=types.SimpleNamespace(id=user_id))
    fake_request = types.SimpleNamespace(state=types.SimpleNamespace(context=None))

    with pytest.raises(HTTPException) as exc:
        await api_mod.delete_action_result(
            request=fake_request, id=run_id, user_res=user_res, db=None,
            space_member=None,
        )

    assert exc.value.status_code == 404
