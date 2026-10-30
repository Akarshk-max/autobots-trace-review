import pytest
from fastapi import HTTPException

from src.autobots.space.space_crud import SpaceMemberCRUD
from src.autobots.space.space_doc_model import (
    SpaceCreate,
    SpaceMemberRole,
    SpaceMemberUpdate,
)
from src.autobots.space.user_space import UserSpaces
from src.autobots.user.user_orm_model import UserORM

MEMBER_USER = UserORM(id="member_user_id", email="member@test.com")
ADMIN_USER = UserORM(id="admin_user_id", email="admin@test.com")
OTHER_USER = UserORM(id="other_user_id", email="other@test.com")


@pytest.mark.asyncio
async def test_add_member(ctx, db, user):
    """Owner can add a member to a space."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(ctx=ctx, space_create=SpaceCreate(name="Add Member"))

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        member = await owner_spaces.add_member(
            ctx=ctx,
            space_id=space.id,
            target_user_id=str(MEMBER_USER.id),
            role=SpaceMemberRole.member,
            caller_membership=owner_membership,
        )
        assert member.role == SpaceMemberRole.member
        assert member.user_id == str(MEMBER_USER.id)
        assert member.invited_by == str(user.id)
    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_add_member_duplicate_rejected(ctx, db, user):
    """Adding an existing member raises 409."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(ctx=ctx, space_create=SpaceCreate(name="Dup Member"))

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        await owner_spaces.add_member(
            ctx=ctx,
            space_id=space.id,
            target_user_id=str(MEMBER_USER.id),
            role=SpaceMemberRole.member,
            caller_membership=owner_membership,
        )
        with pytest.raises(HTTPException) as exc_info:
            await owner_spaces.add_member(
                ctx=ctx,
                space_id=space.id,
                target_user_id=str(MEMBER_USER.id),
                role=SpaceMemberRole.member,
                caller_membership=owner_membership,
            )
        assert exc_info.value.status_code == 409
    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_member_cannot_add_member(ctx, db, user):
    """A member-role user cannot add other members."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(ctx=ctx, space_create=SpaceCreate(name="No Add"))

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        # Add MEMBER_USER as member
        await owner_spaces.add_member(
            ctx=ctx,
            space_id=space.id,
            target_user_id=str(MEMBER_USER.id),
            role=SpaceMemberRole.member,
            caller_membership=owner_membership,
        )
        member_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(MEMBER_USER.id)
        )

        # Member tries to add another user
        member_spaces = UserSpaces(user=MEMBER_USER, db=db)
        with pytest.raises(HTTPException) as exc_info:
            await member_spaces.add_member(
                ctx=ctx,
                space_id=space.id,
                target_user_id=str(OTHER_USER.id),
                role=SpaceMemberRole.member,
                caller_membership=member_membership,
            )
        assert exc_info.value.status_code == 403
    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_admin_can_add_member(ctx, db, user):
    """An admin can add members."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(ctx=ctx, space_create=SpaceCreate(name="Admin Add"))

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        # Add ADMIN_USER as admin
        await owner_spaces.add_member(
            ctx=ctx,
            space_id=space.id,
            target_user_id=str(ADMIN_USER.id),
            role=SpaceMemberRole.admin,
            caller_membership=owner_membership,
        )
        admin_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(ADMIN_USER.id)
        )

        # Admin adds a member
        admin_spaces = UserSpaces(user=ADMIN_USER, db=db)
        member = await admin_spaces.add_member(
            ctx=ctx,
            space_id=space.id,
            target_user_id=str(MEMBER_USER.id),
            role=SpaceMemberRole.member,
            caller_membership=admin_membership,
        )
        assert member.role == SpaceMemberRole.member
    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_list_members(ctx, db, user):
    """List all members of a space."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(ctx=ctx, space_create=SpaceCreate(name="List Members"))

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        await owner_spaces.add_member(
            ctx=ctx,
            space_id=space.id,
            target_user_id=str(MEMBER_USER.id),
            role=SpaceMemberRole.member,
            caller_membership=owner_membership,
        )

        result = await owner_spaces.list_members(ctx=ctx, space_id=space.id)
        assert result.total_count == 2
        user_ids = {m.user_id for m in result.docs}
        assert str(user.id) in user_ids
        assert str(MEMBER_USER.id) in user_ids
    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_update_member_role_owner_only(ctx, db, user):
    """Only owner can change roles."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(
        ctx=ctx, space_create=SpaceCreate(name="Role Change")
    )

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        # Add admin and member
        await owner_spaces.add_member(
            ctx=ctx,
            space_id=space.id,
            target_user_id=str(ADMIN_USER.id),
            role=SpaceMemberRole.admin,
            caller_membership=owner_membership,
        )
        await owner_spaces.add_member(
            ctx=ctx,
            space_id=space.id,
            target_user_id=str(MEMBER_USER.id),
            role=SpaceMemberRole.member,
            caller_membership=owner_membership,
        )

        # Owner promotes member to admin
        updated = await owner_spaces.update_member_role(
            ctx=ctx,
            space_id=space.id,
            target_user_id=str(MEMBER_USER.id),
            role_update=SpaceMemberUpdate(role=SpaceMemberRole.admin),
            caller_membership=owner_membership,
        )
        assert updated.role == SpaceMemberRole.admin

        # Admin cannot change roles
        admin_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(ADMIN_USER.id)
        )
        admin_spaces = UserSpaces(user=ADMIN_USER, db=db)
        with pytest.raises(HTTPException) as exc_info:
            await admin_spaces.update_member_role(
                ctx=ctx,
                space_id=space.id,
                target_user_id=str(MEMBER_USER.id),
                role_update=SpaceMemberUpdate(role=SpaceMemberRole.member),
                caller_membership=admin_membership,
            )
        assert exc_info.value.status_code == 403
    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_cannot_demote_last_owner(ctx, db, user):
    """Cannot demote the only owner."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(
        ctx=ctx, space_create=SpaceCreate(name="Last Owner Demote")
    )

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        with pytest.raises(HTTPException) as exc_info:
            await owner_spaces.update_member_role(
                ctx=ctx,
                space_id=space.id,
                target_user_id=str(user.id),
                role_update=SpaceMemberUpdate(role=SpaceMemberRole.admin),
                caller_membership=owner_membership,
            )
        assert exc_info.value.status_code == 400
    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_remove_member(ctx, db, user):
    """Owner can remove a member."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(
        ctx=ctx, space_create=SpaceCreate(name="Remove Member")
    )

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        await owner_spaces.add_member(
            ctx=ctx,
            space_id=space.id,
            target_user_id=str(MEMBER_USER.id),
            role=SpaceMemberRole.member,
            caller_membership=owner_membership,
        )

        await owner_spaces.remove_member(
            ctx=ctx,
            space_id=space.id,
            target_user_id=str(MEMBER_USER.id),
            caller_membership=owner_membership,
        )

        # Verify removed
        membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(MEMBER_USER.id)
        )
        assert membership is None
    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_member_can_leave(ctx, db, user):
    """A member can remove themselves (leave)."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(ctx=ctx, space_create=SpaceCreate(name="Self Leave"))

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        await owner_spaces.add_member(
            ctx=ctx,
            space_id=space.id,
            target_user_id=str(MEMBER_USER.id),
            role=SpaceMemberRole.member,
            caller_membership=owner_membership,
        )

        member_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(MEMBER_USER.id)
        )
        member_spaces = UserSpaces(user=MEMBER_USER, db=db)
        await member_spaces.remove_member(
            ctx=ctx,
            space_id=space.id,
            target_user_id=str(MEMBER_USER.id),
            caller_membership=member_membership,
        )

        membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(MEMBER_USER.id)
        )
        assert membership is None
    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_cannot_remove_last_owner(ctx, db, user):
    """Cannot remove the only owner from a space."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(
        ctx=ctx, space_create=SpaceCreate(name="Last Owner Remove")
    )

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        with pytest.raises(HTTPException) as exc_info:
            await owner_spaces.remove_member(
                ctx=ctx,
                space_id=space.id,
                target_user_id=str(user.id),
                caller_membership=owner_membership,
            )
        assert exc_info.value.status_code == 400
    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_admin_cannot_remove_owner(ctx, db, user):
    """Admin cannot remove an owner."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(
        ctx=ctx, space_create=SpaceCreate(name="Admin No Remove Owner")
    )

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        await owner_spaces.add_member(
            ctx=ctx,
            space_id=space.id,
            target_user_id=str(ADMIN_USER.id),
            role=SpaceMemberRole.admin,
            caller_membership=owner_membership,
        )

        admin_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(ADMIN_USER.id)
        )
        admin_spaces = UserSpaces(user=ADMIN_USER, db=db)
        with pytest.raises(HTTPException) as exc_info:
            await admin_spaces.remove_member(
                ctx=ctx,
                space_id=space.id,
                target_user_id=str(user.id),
                caller_membership=admin_membership,
            )
        assert exc_info.value.status_code == 403
    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_admin_cannot_add_owner(ctx, db, user):
    """Only owner can add another owner."""
    owner_spaces = UserSpaces(user=user, db=db)
    space = await owner_spaces.create_space(
        ctx=ctx, space_create=SpaceCreate(name="Admin No Add Owner")
    )

    try:
        owner_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        await owner_spaces.add_member(
            ctx=ctx,
            space_id=space.id,
            target_user_id=str(ADMIN_USER.id),
            role=SpaceMemberRole.admin,
            caller_membership=owner_membership,
        )

        admin_membership = await SpaceMemberCRUD(db).find_membership(
            space_id=space.id, user_id=str(ADMIN_USER.id)
        )
        admin_spaces = UserSpaces(user=ADMIN_USER, db=db)
        with pytest.raises(HTTPException) as exc_info:
            await admin_spaces.add_member(
                ctx=ctx,
                space_id=space.id,
                target_user_id=str(OTHER_USER.id),
                role=SpaceMemberRole.owner,
                caller_membership=admin_membership,
            )
        assert exc_info.value.status_code == 403
    finally:
        await owner_spaces.delete_space(ctx=ctx, space_id=space.id)
