"""Two users, one run result: sharing grants read, and its absence denies it.

The existing sharing test only proves positive access; the negative half is what
matters for a result holding someone's data, so both are covered here.
"""

import pytest

from src.autobots.action.action_result.action_result_doc_model import ActionResultFind
from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.event_result.event_result_crud import EventResultCRUD
from src.autobots.event_result.event_result_model import (
    EventResultDocCreate,
    EventResultDocFind,
    EventResultDocUpdate,
    EventResultStatus,
    EventType,
)
from src.autobots.user.user_orm_model import UserORM

RESULT_NAME = "test_shared_run_result"


def _ids(found) -> set[str]:
    return {doc.id for doc in found.docs}


def _result_payload(owner_id: str) -> dict:
    """Must validate as an ActionLiteDoc; an empty dict reads as 'not shared'."""
    return {
        "_id": "test_action_for_share",
        "type": "text2text_llm_chat_openai",
        "name": RESULT_NAME,
        "user_id": owner_id,
        "created_at": "2026-01-01T00:00:00",
    }


@pytest.mark.asyncio
async def test_a_run_result_is_invisible_until_shared_then_readable(
    ctx, db, user, set_test_settings
):
    crud = EventResultCRUD(db)
    user_b = UserORM(id="test_user_b_share_result", email="user_b@runkai.ai")

    created = await crud.insert_one(
        EventResultDocCreate(
            name=RESULT_NAME,
            type=EventType.action_run,
            status=EventResultStatus.success,
            user_id=str(user.id),
            result=_result_payload(str(user.id)),
        )
    )
    assert created is not None
    result_id = created.id

    b_results = UserActionResult(user_b, db)

    try:
        # --- before sharing: B must not reach it by any route -------------------
        assert await b_results.get_owned_or_shared_action_result(result_id) is None, (
            "a result that was never shared must not be readable"
        )

        listed = await b_results.list_owned_or_shared_action_results(
            ActionResultFind(shared_with=user_b.email)
        )
        assert result_id not in _ids(listed), "an unshared result must not be listed"

        # --- grant --------------------------------------------------------------
        updated = await crud.update_one(
            EventResultDocUpdate(
                id=result_id, user_id=str(user.id), shared_with=[user_b.email]
            )
        )
        assert user_b.email in updated.shared_with

        # --- after sharing ------------------------------------------------------
        fetched = await b_results.get_owned_or_shared_action_result(result_id)
        assert fetched is not None, "B should read the result once it is shared"
        assert fetched.user_id == str(user.id), (
            "access came via sharing, not ownership - B does not own this"
        )

        listed = await b_results.list_owned_or_shared_action_results(
            ActionResultFind(shared_with=user_b.email)
        )
        assert result_id in _ids(listed)

        # Sharing is opt-in: without the flag, B's list is still only B's own.
        owner_only = await b_results.list_owned_or_shared_action_results(
            ActionResultFind()
        )
        assert result_id not in _ids(owner_only), (
            "a shared result must not leak into an unfiltered list"
        )

    finally:
        await crud.delete_many(
            EventResultDocFind(id=result_id, user_id=str(user.id))
        )


@pytest.mark.asyncio
async def test_sharing_with_one_user_does_not_expose_it_to_another(
    ctx, db, user, set_test_settings
):
    """The grant is per-grantee, not a global 'shared' bit."""
    crud = EventResultCRUD(db)
    granted = UserORM(id="test_user_granted", email="granted@runkai.ai")
    other = UserORM(id="test_user_other", email="other@runkai.ai")

    created = await crud.insert_one(
        EventResultDocCreate(
            name=RESULT_NAME,
            type=EventType.action_run,
            status=EventResultStatus.success,
            user_id=str(user.id),
            result=_result_payload(str(user.id)),
            shared_with=[granted.email],
        )
    )
    result_id = created.id

    try:
        assert (
            await UserActionResult(granted, db).get_owned_or_shared_action_result(result_id)
            is not None
        )
        assert (
            await UserActionResult(other, db).get_owned_or_shared_action_result(result_id)
            is None
        ), "a user not named in shared_with must not read the result"
    finally:
        await crud.delete_many(
            EventResultDocFind(id=result_id, user_id=str(user.id))
        )
