from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.data_model.context import Context
from src.autobots.space.space_crud import SpaceCRUD, SpaceMemberCRUD
from src.autobots.space.space_doc_model import (
    SpaceCreate,
    SpaceDoc,
    SpaceDocCreate,
    SpaceDocFind,
    SpaceDocsFound,
    SpaceDocUpdate,
    SpaceMemberDoc,
    SpaceMemberDocCreate,
    SpaceMemberDocsFound,
    SpaceMemberRole,
    SpaceMemberUpdate,
    SpaceUpdate,
)
from src.autobots.user.user_orm_model import UserORM


class UserSpaces:
    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
        self.user = user
        self.user_id = str(user.id)
        self.db = db
        self.space_crud = SpaceCRUD(db)
        self.member_crud = SpaceMemberCRUD(db)

    async def create_space(self, ctx: Context, space_create: SpaceCreate) -> SpaceDoc:
        space_doc_create = SpaceDocCreate(
            user_id=self.user_id,
            **space_create.model_dump(),
        )
        space_doc = await self.space_crud.insert_one(space_doc_create)
        if isinstance(space_doc, Exception):
            raise HTTPException(500, "Error creating space")

        member_doc_create = SpaceMemberDocCreate(
            user_id=self.user_id,
            space_id=space_doc.id,
            role=SpaceMemberRole.owner,
            invited_by=self.user_id,
        )
        member_doc = await self.member_crud.insert_one(member_doc_create)
        if isinstance(member_doc, Exception):
            logger.bind(ctx=ctx, space_id=space_doc.id).error(
                "Failed to create owner membership, cleaning up space"
            )
            await self.space_crud.delete_many(SpaceDocFind(id=space_doc.id, user_id=self.user_id))
            raise HTTPException(500, "Error creating space membership")

        logger.bind(ctx=ctx, space_id=space_doc.id).info("Space created")
        return space_doc

    async def list_spaces(self, ctx: Context, limit: int = 100, offset: int = 0) -> SpaceDocsFound:
        memberships = await self.member_crud.find_by_user_id(
            user_id=self.user_id, limit=limit, offset=offset
        )
        space_ids = [m.space_id for m in memberships.docs]
        if not space_ids:
            return SpaceDocsFound(docs=[], total_count=0, limit=limit, offset=offset)

        page = await self.space_crud.find_by_ids(space_ids=space_ids, limit=limit, offset=offset)
        return SpaceDocsFound(
            docs=page.docs, total_count=page.total_count, limit=limit, offset=offset
        )

    async def get_space(self, ctx: Context, space_id: str) -> SpaceDoc:
        membership = await self.member_crud.find_membership(space_id=space_id, user_id=self.user_id)
        if not membership:
            raise HTTPException(403, "Not a member of this space")

        space_doc = await self.space_crud.find_one(SpaceDocFind(id=space_id))
        if not space_doc:
            raise HTTPException(404, "Space not found")
        return space_doc

    async def update_space(
        self, ctx: Context, space_id: str, space_update: SpaceUpdate
    ) -> SpaceDoc:
        membership = await self.member_crud.find_membership(space_id=space_id, user_id=self.user_id)
        if not membership or membership.role != SpaceMemberRole.owner:
            raise HTTPException(403, "Only the owner can update a space")

        space_doc = await self.space_crud.find_one(SpaceDocFind(id=space_id))
        if not space_doc:
            raise HTTPException(404, "Space not found")

        space_doc_update = SpaceDocUpdate(
            id=space_id,
            user_id=space_doc.user_id,
            **space_update.model_dump(exclude_none=True),
        )
        updated = await self.space_crud.update_one(space_doc_update)
        if isinstance(updated, Exception):
            raise HTTPException(500, "Error updating space")

        logger.bind(ctx=ctx, space_id=space_id).info("Space updated")
        return updated

    async def delete_space(self, ctx: Context, space_id: str) -> None:
        membership = await self.member_crud.find_membership(space_id=space_id, user_id=self.user_id)
        if not membership or membership.role != SpaceMemberRole.owner:
            raise HTTPException(403, "Only the owner can delete a space")

        space_doc = await self.space_crud.find_one(SpaceDocFind(id=space_id))
        if not space_doc:
            raise HTTPException(404, "Space not found")

        await self.member_crud.delete_by_space_id(space_id)
        # Clean up invitations
        from src.autobots.space.space_invitation_crud import SpaceInvitationCRUD

        await SpaceInvitationCRUD(self.db).delete_by_space_id(space_id)
        await self.space_crud.delete_many(SpaceDocFind(id=space_id, user_id=space_doc.user_id))
        logger.bind(ctx=ctx, space_id=space_id).info("Space deleted")

    # --- Membership Management ---

    async def add_member(
        self,
        ctx: Context,
        space_id: str,
        target_user_id: str,
        role: SpaceMemberRole,
        caller_membership: SpaceMemberDoc,
    ) -> SpaceMemberDoc:
        if caller_membership.role not in (SpaceMemberRole.owner, SpaceMemberRole.admin):
            raise HTTPException(403, "Admin or owner role required to add members")

        if role == SpaceMemberRole.owner and caller_membership.role != SpaceMemberRole.owner:
            raise HTTPException(403, "Only an owner can add another owner")

        existing = await self.member_crud.find_membership(space_id=space_id, user_id=target_user_id)
        if existing:
            raise HTTPException(409, "User is already a member of this space")

        member_doc_create = SpaceMemberDocCreate(
            user_id=target_user_id,
            space_id=space_id,
            role=role,
            invited_by=self.user_id,
        )
        member = await self.member_crud.insert_one(member_doc_create)
        if isinstance(member, Exception):
            raise HTTPException(500, "Error adding member")

        logger.bind(ctx=ctx, space_id=space_id, target_user_id=target_user_id).info(
            "Member added to space"
        )
        return member

    async def list_members(
        self,
        ctx: Context,
        space_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> SpaceMemberDocsFound:
        page = await self.member_crud.find_by_space_id(
            space_id=space_id, limit=limit, offset=offset
        )
        return SpaceMemberDocsFound(
            docs=page.docs, total_count=page.total_count, limit=limit, offset=offset
        )

    async def update_member_role(
        self,
        ctx: Context,
        space_id: str,
        target_user_id: str,
        role_update: SpaceMemberUpdate,
        caller_membership: SpaceMemberDoc,
    ) -> SpaceMemberDoc:
        if caller_membership.role != SpaceMemberRole.owner:
            raise HTTPException(403, "Only an owner can change member roles")

        target_membership = await self.member_crud.find_membership(
            space_id=space_id, user_id=target_user_id
        )
        if not target_membership:
            raise HTTPException(404, "Member not found in this space")

        # Prevent demoting the last owner
        if (
            target_membership.role == SpaceMemberRole.owner
            and role_update.role != SpaceMemberRole.owner
        ):
            owner_count = await self.member_crud.count_owners(space_id)
            if owner_count <= 1:
                raise HTTPException(400, "Cannot demote the last owner")

        from src.autobots.space.space_doc_model import SpaceMemberDocUpdate

        doc_update = SpaceMemberDocUpdate(
            id=target_membership.id,
            user_id=target_membership.user_id,
            **role_update.model_dump(exclude_none=True),
        )
        updated = await self.member_crud.update_one(doc_update)
        if isinstance(updated, Exception):
            raise HTTPException(500, "Error updating member role")

        logger.bind(ctx=ctx, space_id=space_id, target_user_id=target_user_id).info(
            "Member role updated"
        )
        return updated

    async def remove_member(
        self,
        ctx: Context,
        space_id: str,
        target_user_id: str,
        caller_membership: SpaceMemberDoc,
    ) -> None:
        is_self = self.user_id == target_user_id

        if not is_self:
            if caller_membership.role not in (SpaceMemberRole.owner, SpaceMemberRole.admin):
                raise HTTPException(403, "Admin or owner role required to remove members")

        target_membership = await self.member_crud.find_membership(
            space_id=space_id, user_id=target_user_id
        )
        if not target_membership:
            raise HTTPException(404, "Member not found in this space")

        # Admin cannot remove an owner
        if (
            target_membership.role == SpaceMemberRole.owner
            and caller_membership.role != SpaceMemberRole.owner
        ):
            raise HTTPException(403, "Only an owner can remove another owner")

        # Cannot remove the last owner
        if target_membership.role == SpaceMemberRole.owner:
            owner_count = await self.member_crud.count_owners(space_id)
            if owner_count <= 1:
                raise HTTPException(400, "Cannot remove the last owner")

        from src.autobots.space.space_doc_model import SpaceMemberDocFind

        await self.member_crud.delete_many(
            SpaceMemberDocFind(id=target_membership.id, user_id=target_user_id, space_id=space_id)
        )
        logger.bind(ctx=ctx, space_id=space_id, target_user_id=target_user_id).info(
            "Member removed from space"
        )
