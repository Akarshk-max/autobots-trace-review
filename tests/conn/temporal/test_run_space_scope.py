from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError

from src.autobots.conn.temporal import resolve_user
from src.autobots.conn.temporal.action.shared import ActionWorkflowInput
from src.autobots.conn.temporal.action_graph.shared import ActionGraphWorkflowInput
from src.autobots.conn.temporal.action_team.shared import ActionTeamWorkflowInput
from src.autobots.conn.temporal.resolve_user import resolve_run_user
from src.autobots.conn.temporal.tool.shared import ToolWorkflowInput
from src.autobots.space.space_crud import SpaceMemberCRUD
from src.autobots.space.space_doc_model import SpaceMemberDocCreate, SpaceMemberRole

PAYLOADS = [
    (ActionWorkflowInput, {"action_id": "a1", "action_run_id": "r1"}),
    (ActionGraphWorkflowInput, {"action_graph_id": "g1", "action_graph_result_id": "r1"}),
    (ActionTeamWorkflowInput, {"action_team_id": "t1", "action_team_result_id": "r1"}),
    (ToolWorkflowInput, {"tool_run_id": "r1", "tool_name": "get_date_time"}),
]


@pytest.fixture
def stored_user(user, monkeypatch):
    async def _get(**kwargs):
        return user

    monkeypatch.setattr(resolve_user.User, "get", staticmethod(_get))
    return user


@pytest_asyncio.fixture
async def membership(db, user):
    space_id = f"space_{uuid.uuid4().hex[:8]}"
    await SpaceMemberCRUD(db).insert_one(
        SpaceMemberDocCreate(
            space_id=space_id,
            user_id=str(user.id),
            role=SpaceMemberRole.member,
            invited_by=str(user.id),
        )
    )
    yield space_id
    deleted = await SpaceMemberCRUD(db).document.delete_one(
        {"space_id": space_id, "user_id": str(user.id)}
    )
    assert deleted.deleted_count == 1


@pytest.mark.asyncio
async def test_a_run_in_a_space_resolves_the_membership(ctx, db, stored_user, membership):
    resolved = await resolve_run_user(
        ctx=ctx, db=db, user_id=str(stored_user.id), space_id=membership
    )

    assert resolved.space_member is not None
    assert resolved.space_member.space_id == membership


@pytest.mark.asyncio
async def test_a_personal_run_resolves_without_a_space(ctx, db, stored_user):
    resolved = await resolve_run_user(ctx=ctx, db=db, user_id=str(stored_user.id), space_id=None)

    assert resolved.space_member is None


@pytest.mark.asyncio
async def test_a_revoked_membership_fails_instead_of_running_personally(ctx, db, stored_user):
    with pytest.raises(HTTPException) as raised:
        await resolve_run_user(
            ctx=ctx, db=db, user_id=str(stored_user.id), space_id="space_never_joined"
        )

    assert raised.value.status_code == 403


@pytest.mark.parametrize("model,ids", PAYLOADS)
def test_every_workflow_payload_must_state_its_space(model, ids):
    with pytest.raises(ValidationError):
        model(**ids, user_id="u")

    assert model(**ids, user_id="u", space_id=None).space_id is None
