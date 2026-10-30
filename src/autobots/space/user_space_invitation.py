from datetime import datetime

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.data_model.context import Context
from src.autobots.space.space_crud import SpaceMemberCRUD
from src.autobots.space.space_doc_model import SpaceMemberDoc, SpaceMemberDocCreate, SpaceMemberRole
from src.autobots.space.space_invitation_crud import SpaceInvitationCRUD
from src.autobots.space.space_invitation_doc_model import (
    InvitationStatus,
    SpaceInvitationCreate,
    SpaceInvitationDoc,
    SpaceInvitationDocCreate,
    SpaceInvitationDocsFound,
)
from src.autobots.user.user_orm_model import UserORM


class UserSpaceInvitations:
    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
        self.user = user
        self.user_id = str(user.id)
        self.user_email = user.email
        self.db = db
        self.invitation_crud = SpaceInvitationCRUD(db)
        self.member_crud = SpaceMemberCRUD(db)

    async def send_invitation(
        self,
        ctx: Context,
        space_id: str,
        invitation_create: SpaceInvitationCreate,
        caller_membership: SpaceMemberDoc,
    ) -> SpaceInvitationDoc:
        if caller_membership.role not in (SpaceMemberRole.owner, SpaceMemberRole.admin):
            raise HTTPException(403, "Admin or owner role required to invite")

        if (
            invitation_create.role == SpaceMemberRole.owner
            and caller_membership.role != SpaceMemberRole.owner
        ):
            raise HTTPException(403, "Only an owner can invite as owner")

        # Check for duplicate pending invitation
        existing = await self.invitation_crud.find_pending_duplicate(
            space_id=space_id, invitee_email=invitation_create.invitee_email
        )
        if existing:
            raise HTTPException(409, "A pending invitation already exists for this email")

        doc_create = SpaceInvitationDocCreate(
            space_id=space_id,
            invitee_email=invitation_create.invitee_email,
            role=invitation_create.role,
            invited_by=self.user_id,
        )
        invitation = await self.invitation_crud.insert_one(doc_create)
        if isinstance(invitation, Exception):
            raise HTTPException(500, "Error creating invitation")

        logger.bind(ctx=ctx, space_id=space_id, invitee_email=invitation_create.invitee_email).info(
            "Invitation sent"
        )
        return invitation

    async def list_space_invitations(
        self,
        ctx: Context,
        space_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> SpaceInvitationDocsFound:
        page = await self.invitation_crud.find_pending_by_space(
            space_id=space_id, limit=limit, offset=offset
        )
        return SpaceInvitationDocsFound(
            docs=page.docs, total_count=page.total_count, limit=limit, offset=offset
        )

    async def revoke_invitation(
        self,
        ctx: Context,
        space_id: str,
        invitation_id: str,
        caller_membership: SpaceMemberDoc,
    ) -> None:
        if caller_membership.role not in (SpaceMemberRole.owner, SpaceMemberRole.admin):
            raise HTTPException(403, "Admin or owner role required to revoke invitations")

        invitation = await self.invitation_crud.find_by_id(invitation_id)
        if not invitation or invitation.space_id != space_id:
            raise HTTPException(404, "Invitation not found")
        if invitation.status != InvitationStatus.pending:
            raise HTTPException(400, "Can only revoke pending invitations")

        await self.invitation_crud.update_status(
            invitation_id=invitation_id,
            invited_by=invitation.invited_by,
            status=InvitationStatus.declined,
        )
        logger.bind(ctx=ctx, invitation_id=invitation_id).info("Invitation revoked")

    async def list_my_invitations(
        self,
        ctx: Context,
        limit: int = 100,
        offset: int = 0,
    ) -> SpaceInvitationDocsFound:
        page = await self.invitation_crud.find_pending_by_email(
            email=self.user_email, limit=limit, offset=offset
        )
        return SpaceInvitationDocsFound(
            docs=page.docs, total_count=page.total_count, limit=limit, offset=offset
        )

    async def accept_invitation(self, ctx: Context, invitation_id: str) -> SpaceMemberDoc:
        invitation = await self.invitation_crud.find_by_id(invitation_id)
        if not invitation:
            raise HTTPException(404, "Invitation not found")
        if invitation.invitee_email != self.user_email:
            raise HTTPException(403, "This invitation is not for you")
        if invitation.status != InvitationStatus.pending:
            raise HTTPException(400, "Invitation is no longer pending")
        if invitation.expires_at < datetime.now():
            await self.invitation_crud.update_status(
                invitation_id=invitation_id,
                invited_by=invitation.invited_by,
                status=InvitationStatus.expired,
            )
            raise HTTPException(400, "Invitation has expired")

        # Check if already a member
        existing = await self.member_crud.find_membership(
            space_id=invitation.space_id, user_id=self.user_id
        )
        if existing:
            await self.invitation_crud.update_status(
                invitation_id=invitation_id,
                invited_by=invitation.invited_by,
                status=InvitationStatus.accepted,
            )
            raise HTTPException(409, "Already a member of this space")

        # Create membership
        member_create = SpaceMemberDocCreate(
            user_id=self.user_id,
            space_id=invitation.space_id,
            role=invitation.role,
            invited_by=invitation.invited_by,
        )
        member = await self.member_crud.insert_one(member_create)
        if isinstance(member, Exception):
            raise HTTPException(500, "Error creating membership")

        # Mark invitation as accepted
        await self.invitation_crud.update_status(
            invitation_id=invitation_id,
            invited_by=invitation.invited_by,
            status=InvitationStatus.accepted,
        )

        logger.bind(ctx=ctx, invitation_id=invitation_id, space_id=invitation.space_id).info(
            "Invitation accepted"
        )
        return member

    async def decline_invitation(self, ctx: Context, invitation_id: str) -> None:
        invitation = await self.invitation_crud.find_by_id(invitation_id)
        if not invitation:
            raise HTTPException(404, "Invitation not found")
        if invitation.invitee_email != self.user_email:
            raise HTTPException(403, "This invitation is not for you")
        if invitation.status != InvitationStatus.pending:
            raise HTTPException(400, "Invitation is no longer pending")

        await self.invitation_crud.update_status(
            invitation_id=invitation_id,
            invited_by=invitation.invited_by,
            status=InvitationStatus.declined,
        )
        logger.bind(ctx=ctx, invitation_id=invitation_id).info("Invitation declined")
