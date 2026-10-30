"""The gate answers the same way whichever kind of run is asked.

`authorize_turn` was written for action runs and lifted so team and graph runs
could take a turn from anyone but their owner. A lift that only works for the
type it came from is the failure worth guarding, so every service that adopts
the mixin is put through the same three questions here.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from fastapi import HTTPException

import src.autobots.event_result.turn_authorization as turn_auth_mod
from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
    UserActionGraphResult,
)
from src.autobots.action_team.action_team_result.user_action_team_result import (
    UserActionTeamResult,
)
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import Participation
from src.autobots.space.space_doc_model import SpaceMemberDoc, SpaceMemberRole
from src.autobots.user.user_orm_model import UserDoc, UserORM

OWNER = "owner-1"
GRANTEE = "grantee-2"
RUN = "run-1"
SPACE = "space-9"

SERVICES = [UserActionResult, UserActionTeamResult, UserActionGraphResult]
IDS = ["action", "team", "graph"]

#: The lookup each service hands `_owned_or_shared_run` on to.
LOOKUPS = {
    UserActionResult: "get_owned_or_shared_action_result",
    UserActionTeamResult: "get_owned_or_shared_action_team_result",
    UserActionGraphResult: "get_owned_or_shared_action_graph_result",
}


class _Run:
    """Only the two fields the gate reads."""

    def __init__(self, participation: Participation, owner: str = OWNER):
        self.user_id = owner
        self.participation = participation


def _service(service_cls, user_id: str, found, space: SpaceMemberDoc | None = None):
    """A service with its dependencies bypassed, since none of them are the subject."""
    svc = service_cls.__new__(service_cls)
    svc.user = UserORM(id=user_id, email=f"{user_id}@example.com", space_member=space)
    svc.user_id = user_id
    svc.space_member = space

    async def _found(run_id: str, ctx: Any = None):
        return found

    svc._owned_or_shared_run = _found
    return svc


@pytest.fixture(autouse=True)
def owner_lookup(monkeypatch: pytest.MonkeyPatch):
    """The gate loads the owner to build their service object."""

    class _User:
        @staticmethod
        async def get(ctx: Any, user: UserORM, id: str, db: Any) -> UserDoc:
            return UserDoc.model_construct(_id=id, id=id, user_id=id, email=f"{id}@example.com")

    monkeypatch.setattr(turn_auth_mod, "User", _User)
    # Each service builds these in __init__, and the owner's is built inside the
    # gate. None of them are what is under test.
    for module in (
        "src.autobots.action.action_result.user_action_result",
        "src.autobots.action_team.action_team_result.user_action_team_result",
        "src.autobots.action_graph.action_graph_result.user_action_graph_result",
    ):
        monkeypatch.setattr(f"{module}.EventResultCRUD", lambda db: None)
        monkeypatch.setattr(f"{module}.UserFiles", lambda **kw: None, raising=False)
        monkeypatch.setattr(f"{module}.get_s3", lambda: None)


@pytest.mark.parametrize("service_cls", SERVICES, ids=IDS)
@pytest.mark.asyncio
async def test_a_participant_acts_as_the_owner(service_cls):
    """The point of the whole thing: authorised as the caller, acting as the owner."""
    caller = _service(service_cls, GRANTEE, _Run(Participation.post_as_owner))

    _, as_owner = await caller.authorize_turn(
        run_id=RUN, db=None, ctx=Context(user_id=GRANTEE)
    )

    assert as_owner is not caller
    assert as_owner.user_id == OWNER
    # And the same kind of service, or the run would be written by something that
    # does not know where this type keeps its conversation.
    assert isinstance(as_owner, service_cls)


@pytest.mark.parametrize("service_cls", SERVICES, ids=IDS)
@pytest.mark.asyncio
async def test_a_read_only_run_refuses_a_participant(service_cls):
    caller = _service(service_cls, GRANTEE, _Run(Participation.read_only))

    with pytest.raises(HTTPException) as excinfo:
        await caller.authorize_turn(run_id=RUN, db=None, ctx=Context(user_id=GRANTEE))

    assert excinfo.value.status_code == 403


@pytest.mark.parametrize("service_cls", SERVICES, ids=IDS)
@pytest.mark.asyncio
async def test_a_stranger_gets_nothing(service_cls):
    """404, not 403 — whether the run exists is not a stranger's business."""
    caller = _service(service_cls, GRANTEE, None)

    with pytest.raises(HTTPException) as excinfo:
        await caller.authorize_turn(run_id=RUN, db=None, ctx=Context(user_id=GRANTEE))

    assert excinfo.value.status_code == 404
    # Each type names what it could not find, rather than all saying "run".
    assert excinfo.value.detail == service_cls._RUN_NOT_FOUND


@pytest.mark.parametrize("service_cls", SERVICES, ids=IDS)
@pytest.mark.asyncio
async def test_the_owner_acts_as_themselves(service_cls):
    """No owner lookup, no second service — the caller already is the owner."""
    caller = _service(service_cls, OWNER, _Run(Participation.read_only))

    _, as_owner = await caller.authorize_turn(
        run_id=RUN, db=None, ctx=Context(user_id=OWNER)
    )

    assert as_owner is caller


@pytest.mark.parametrize("service_cls", SERVICES, ids=IDS)
@pytest.mark.asyncio
async def test_the_owners_service_keeps_the_callers_space(service_cls):
    """A find carrying no space is not unscoped: `_build_filter` injects
    `space_id: None`, so an owner service without membership sees personal runs
    and nothing else. The space belongs to the request, not the identity."""
    space = SpaceMemberDoc.model_construct(
        space_id=SPACE, user_id=GRANTEE, role=SpaceMemberRole.member
    )
    caller = _service(service_cls, GRANTEE, _Run(Participation.post_as_owner), space=space)

    _, as_owner = await caller.authorize_turn(
        run_id=RUN, db=None, ctx=Context(user_id=GRANTEE)
    )

    assert as_owner.space_member is not None, "the owner's service lost the space"
    assert as_owner.space_member.space_id == SPACE


@pytest.mark.parametrize("service_cls", SERVICES, ids=IDS)
@pytest.mark.asyncio
async def test_the_run_id_reaches_the_lookup(service_cls):
    """The one thing every test above stubs out.

    They all replace `_owned_or_shared_run`, so nothing here ever ran the real
    one - and its three implementations disagree on argument order. Two of the
    lookups take `(ctx, id)` while the mixin hands over `(run_id, ctx)`, so
    called positionally the run id arrives as the context and the lookup goes
    looking for a `Context` object. It finds nothing, the gate reads that as
    "not yours", and every resume of a shared-capable run 404s.
    """
    lookup_name = LOOKUPS[service_cls]
    svc = service_cls.__new__(service_cls)
    seen: dict[str, Any] = {}

    async def _record(*args, **kwargs):
        bound = inspect.signature(getattr(service_cls, lookup_name)).bind(
            svc, *args, **kwargs
        )
        seen.update(bound.arguments)
        return None

    setattr(svc, lookup_name, _record)

    await svc._owned_or_shared_run(RUN, Context(user_id=OWNER))

    (id_param,) = [name for name in seen if name.endswith("_id")]
    assert seen[id_param] == RUN, f"{lookup_name} was passed {seen[id_param]!r} as the run id"
    assert isinstance(seen.get("ctx"), Context)
