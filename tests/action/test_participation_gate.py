"""Who may add to a run, and whose identity does the work.

`authorize_turn` answers both. The second half is the one worth guarding: a
`UserActionResult` carries its user into the Mongo filter and the S3 key prefix,
so handing back the caller's would write the owner's conversation into the
caller's storage — a successful write to the wrong place.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

import src.autobots.action.action_result.user_action_result as uar_mod
import src.autobots.event_result.turn_authorization as turn_auth_mod
from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultDocFind, Participation
from src.autobots.space.space_doc_model import SpaceMemberDoc, SpaceMemberRole
from src.autobots.user.user_orm_model import UserDoc, UserORM

OWNER = "owner-1"
GRANTEE = "grantee-2"
RUN = "run-1"
SPACE = "space-9"


def _run(participation: Participation, owner: str = OWNER):
    doc = uar_mod.ActionResultDoc.model_construct(
        _id=RUN, id=RUN, user_id=owner, participation=participation
    )
    return doc


def _service(user_id: str, found) -> UserActionResult:
    svc = UserActionResult.__new__(UserActionResult)
    svc.user = UserORM(id=user_id, email=f"{user_id}@example.com")
    svc.user_id = user_id
    svc.space_member = None

    async def _found(action_result_id: str, ctx: Any = None):
        return found

    svc.get_owned_or_shared_action_result = _found
    return svc


@pytest.fixture(autouse=True)
def owner_lookup(monkeypatch: pytest.MonkeyPatch):
    """`authorize_turn` loads the owner to build their service object."""

    class _User:
        @staticmethod
        async def get(ctx: Any, user: UserORM, id: str, db: Any) -> UserDoc:
            return UserDoc.model_construct(_id=id, id=id, user_id=id, email=f"{id}@example.com")

    # The owner lookup moved with `authorize_turn` when it was lifted, so this
    # patches where the call now lives rather than where it used to.
    monkeypatch.setattr(turn_auth_mod, "User", _User)
    monkeypatch.setattr(uar_mod, "EventResultCRUD", lambda db: None)
    # `raising=False`: the run payload store stops going through `UserFiles`
    # once the flat-key branch lands, and this fixture predates it.
    monkeypatch.setattr(uar_mod, "UserFiles", lambda **kw: None, raising=False)
    monkeypatch.setattr(uar_mod, "get_s3", lambda: None)


async def _authorize(caller: str, run) -> tuple:
    return await _service(caller, run).authorize_turn(
        run_id=RUN, db=None, ctx=Context(user_id=caller)
    )


@pytest.mark.asyncio
async def test_the_owner_acts_as_themselves():
    run, as_user = await _authorize(OWNER, _run(Participation.read_only))

    assert as_user.user_id == OWNER
    assert run.user_id == OWNER


@pytest.mark.asyncio
async def test_a_participant_acts_as_the_owner():
    """The point of the whole step: the write must land in the owner's storage."""
    run, as_user = await _authorize(GRANTEE, _run(Participation.post_as_owner))

    assert as_user.user_id == OWNER, "a grantee's service would write to their own prefix"
    assert run.user_id == OWNER


@pytest.mark.asyncio
async def test_a_read_only_run_refuses_a_participant():
    with pytest.raises(HTTPException) as caught:
        await _authorize(GRANTEE, _run(Participation.read_only))

    assert caught.value.status_code == 403


def test_read_only_is_what_an_untouched_run_gets():
    """Every run that exists predates the field, so the default is the gate."""
    from src.autobots.event_result.event_result_model import EventResultDoc

    doc = EventResultDoc.model_validate(
        {"_id": RUN, "user_id": OWNER, "type": "action_run", "status": "success", "result": {}}
    )
    assert doc.participation == Participation.read_only


@pytest.mark.asyncio
async def test_post_as_self_admits_the_participant_and_still_writes_as_the_owner():
    """Resolving the actor's connections does not move the run.

    Whose resources a turn uses and where it is written are separate questions;
    only the first one changes in this mode.
    """
    run, as_user = await _authorize(GRANTEE, _run(Participation.post_as_self))

    assert as_user.user_id == OWNER, "the turn would write into the grantee's storage"
    assert run.participation == Participation.post_as_self


@pytest.mark.asyncio
async def test_a_stranger_gets_nothing():
    with pytest.raises(HTTPException) as caught:
        await _authorize("stranger-3", None)

    assert caught.value.status_code == 404


def _member(user_id: str) -> SpaceMemberDoc:
    return SpaceMemberDoc.model_construct(
        _id="m-1", id="m-1", user_id=user_id, space_id=SPACE, role=SpaceMemberRole.member
    )


@pytest.mark.asyncio
async def test_the_owners_service_keeps_the_callers_space():
    """Without it the feature works only outside spaces, and says 404 inside one.

    A find that carries no space is not unscoped: `_build_filter` injects
    `space_id: None`. The stored user has no live membership, so a service built
    from them alone sees personal runs and nothing else - the turn would clear
    this gate and then fail in `init` as a 400.
    """
    caller = _service(GRANTEE, _run(Participation.post_as_owner))
    caller.user.space_member = _member(GRANTEE)
    caller.space_member = caller.user.space_member

    _, as_owner = await caller.authorize_turn(
        run_id=RUN, db=None, ctx=Context(user_id=GRANTEE)
    )

    assert as_owner.user_id == OWNER
    assert as_owner.space_member is not None, "the owner's service lost the space"

    # The filter is what actually broke, so it is what gets pinned.
    find = EventResultDocFind(
        id=RUN, user_id=as_owner.user_id, space_id=as_owner.space_member.space_id
    )
    built = await CRUDBase._build_filter(CRUDBase.__new__(CRUDBase), find)
    assert built["space_id"] == SPACE, "a run inside a space would be invisible"


@pytest.mark.asyncio
async def test_a_personal_run_stays_personal():
    caller = _service(GRANTEE, _run(Participation.post_as_owner))

    _, as_owner = await caller.authorize_turn(
        run_id=RUN, db=None, ctx=Context(user_id=GRANTEE)
    )

    assert as_owner.space_member is None
