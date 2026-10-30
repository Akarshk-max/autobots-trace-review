from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from src.autobots.space.space_crud import SpaceMemberCRUD
from src.autobots.space.space_doc_model import SpaceCreate, SpaceMemberRole
from src.autobots.space.space_invitation_crud import SpaceInvitationCRUD
from src.autobots.space.space_invitation_doc_model import (
    InvitationStatus,
    SpaceInvitationCreate,
)
from src.autobots.space.user_space import UserSpaces
from src.autobots.space.user_space_invitation import UserSpaceInvitations
from src.autobots.user.user_orm_model import UserORM

INVITEE_USER = UserORM(id="invitee_user_id", email="invitee@test.com")
OTHER_USER = UserORM(id="other_user_id", email="other@test.com")


@pytest.mark.asyncio
async def test_send_and_accept_invitation(ctx, db, user):
    """Full invite-accept flow: send invitation, invitee accepts, membership created."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(
        ctx=ctx, space_create=SpaceCreate(name="Invite Accept")
    )

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        owner_invitations = UserSpaceInvitations(user=user, db=db)

        # Send invitation
        invitation = await owner_invitations.send_invitation(
            ctx=ctx,
            space_id=space.id,
            invitation_create=SpaceInvitationCreate(
                invitee_email=INVITEE_USER.email, role=SpaceMemberRole.member
            ),
            caller_membership=owner_membership,
        )
        assert invitation.status == InvitationStatus.pending
        assert invitation.invitee_email == INVITEE_USER.email

        # Invitee sees it in their list
        invitee_invitations = UserSpaceInvitations(user=INVITEE_USER, db=db)
        my_invites = await invitee_invitations.list_my_invitations(ctx=ctx)
        assert my_invites.total_count >= 1
        invite_ids = {i.id for i in my_invites.docs}
        assert invitation.id in invite_ids

        # Invitee accepts
        member = await invitee_invitations.accept_invitation(
            ctx=ctx, invitation_id=invitation.id
        )
        assert member.role == SpaceMemberRole.member
        assert member.user_id == str(INVITEE_USER.id)
        assert member.space_id == space.id

        # Invitation is now accepted
        updated_invite = await SpaceInvitationCRUD(db).find_by_id(invitation.id)
        assert updated_invite.status == InvitationStatus.accepted

    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_decline_invitation(ctx, db, user):
    """Invitee can decline an invitation."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(
        ctx=ctx, space_create=SpaceCreate(name="Invite Decline")
    )

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        owner_invitations = UserSpaceInvitations(user=user, db=db)

        invitation = await owner_invitations.send_invitation(
            ctx=ctx,
            space_id=space.id,
            invitation_create=SpaceInvitationCreate(invitee_email=INVITEE_USER.email),
            caller_membership=owner_membership,
        )

        invitee_invitations = UserSpaceInvitations(user=INVITEE_USER, db=db)
        await invitee_invitations.decline_invitation(ctx=ctx, invitation_id=invitation.id)

        updated = await SpaceInvitationCRUD(db).find_by_id(invitation.id)
        assert updated.status == InvitationStatus.declined

        # Invitee is NOT a member
        membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(INVITEE_USER.id)
        )
        assert membership is None

    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_duplicate_invitation_rejected(ctx, db, user):
    """Cannot send a second pending invitation to the same email for the same space."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(
        ctx=ctx, space_create=SpaceCreate(name="Invite Dup")
    )

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        owner_invitations = UserSpaceInvitations(user=user, db=db)

        await owner_invitations.send_invitation(
            ctx=ctx,
            space_id=space.id,
            invitation_create=SpaceInvitationCreate(invitee_email=INVITEE_USER.email),
            caller_membership=owner_membership,
        )

        with pytest.raises(HTTPException) as exc_info:
            await owner_invitations.send_invitation(
                ctx=ctx,
                space_id=space.id,
                invitation_create=SpaceInvitationCreate(invitee_email=INVITEE_USER.email),
                caller_membership=owner_membership,
            )
        assert exc_info.value.status_code == 409

    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_revoke_invitation(ctx, db, user):
    """Owner/Admin can revoke a pending invitation."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(
        ctx=ctx, space_create=SpaceCreate(name="Invite Revoke")
    )

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        owner_invitations = UserSpaceInvitations(user=user, db=db)

        invitation = await owner_invitations.send_invitation(
            ctx=ctx,
            space_id=space.id,
            invitation_create=SpaceInvitationCreate(invitee_email=INVITEE_USER.email),
            caller_membership=owner_membership,
        )

        await owner_invitations.revoke_invitation(
            ctx=ctx,
            space_id=space.id,
            invitation_id=invitation.id,
            caller_membership=owner_membership,
        )

        updated = await SpaceInvitationCRUD(db).find_by_id(invitation.id)
        assert updated.status == InvitationStatus.declined

    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_wrong_user_cannot_accept(ctx, db, user):
    """A user who is not the invitee cannot accept the invitation."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(
        ctx=ctx, space_create=SpaceCreate(name="Wrong Accept")
    )

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        owner_invitations = UserSpaceInvitations(user=user, db=db)

        invitation = await owner_invitations.send_invitation(
            ctx=ctx,
            space_id=space.id,
            invitation_create=SpaceInvitationCreate(invitee_email=INVITEE_USER.email),
            caller_membership=owner_membership,
        )

        # OTHER_USER (wrong email) tries to accept
        other_invitations = UserSpaceInvitations(user=OTHER_USER, db=db)
        with pytest.raises(HTTPException) as exc_info:
            await other_invitations.accept_invitation(ctx=ctx, invitation_id=invitation.id)
        assert exc_info.value.status_code == 403

    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_expired_invitation_cannot_be_accepted(ctx, db, user):
    """An expired invitation is rejected on accept."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(
        ctx=ctx, space_create=SpaceCreate(name="Expired Invite")
    )

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        owner_invitations = UserSpaceInvitations(user=user, db=db)

        invitation = await owner_invitations.send_invitation(
            ctx=ctx,
            space_id=space.id,
            invitation_create=SpaceInvitationCreate(invitee_email=INVITEE_USER.email),
            caller_membership=owner_membership,
        )

        # Manually expire the invitation
        crud = SpaceInvitationCRUD(db)
        await crud.document.update_one(
            {"_id": __import__("bson").ObjectId(invitation.id)},
            {"$set": {"expires_at": datetime.now() - timedelta(days=1)}},
        )

        invitee_invitations = UserSpaceInvitations(user=INVITEE_USER, db=db)
        with pytest.raises(HTTPException) as exc_info:
            await invitee_invitations.accept_invitation(ctx=ctx, invitation_id=invitation.id)
        assert exc_info.value.status_code == 400

    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_list_space_invitations(ctx, db, user):
    """Owner/Admin can list pending invitations for a space."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(
        ctx=ctx, space_create=SpaceCreate(name="List Invites")
    )

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        owner_invitations = UserSpaceInvitations(user=user, db=db)

        await owner_invitations.send_invitation(
            ctx=ctx,
            space_id=space.id,
            invitation_create=SpaceInvitationCreate(invitee_email="a@test.com"),
            caller_membership=owner_membership,
        )
        await owner_invitations.send_invitation(
            ctx=ctx,
            space_id=space.id,
            invitation_create=SpaceInvitationCreate(invitee_email="b@test.com"),
            caller_membership=owner_membership,
        )

        result = await owner_invitations.list_space_invitations(ctx=ctx, space_id=space.id)
        assert result.total_count == 2

    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_delete_space_cleans_up_invitations(ctx, db, user):
    """Deleting a space removes all its invitations."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(
        ctx=ctx, space_create=SpaceCreate(name="Delete Cleanup")
    )

    owner_membership = await SpaceMemberCRUD(db).find_membership(
        space_id=space.id, user_id=str(user.id)
    )
    owner_invitations = UserSpaceInvitations(user=user, db=db)

    invitation = await owner_invitations.send_invitation(
        ctx=ctx,
        space_id=space.id,
        invitation_create=SpaceInvitationCreate(invitee_email=INVITEE_USER.email),
        caller_membership=owner_membership,
    )

    await owner_spaces.delete_space(ctx=ctx, space_id=space.id)

    # Invitation should be gone
    found = await SpaceInvitationCRUD(db).find_by_id(invitation.id)
    assert found is None
