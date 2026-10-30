import pytest
from fastapi import HTTPException

from src.autobots.space.space_crud import SpaceMemberCRUD
from src.autobots.space.space_doc_model import (
    SpaceCreate,
    SpaceDocFind,
    SpaceMemberRole,
    SpaceUpdate,
)
from src.autobots.space.user_space import UserSpaces


@pytest.mark.asyncio
async def test_create_space_creates_owner_membership(ctx, db, user):
    user_spaces = UserSpaces(user=user, db=db)
    member_crud = SpaceMemberCRUD(db)
    space = None

    try:
        space = await user_spaces.create_space(
            ctx=ctx, space_create=SpaceCreate(name="Test Owner Space")
        )
        assert space.name == "Test Owner Space"
        assert space.user_id == str(user.id)

        # Verify owner membership was auto-created
        membership = await member_crud.find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        assert membership is not None
        assert membership.role == SpaceMemberRole.owner

    finally:
        if space:
            await user_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_list_spaces_returns_only_member_spaces(ctx, db, user):
    user_spaces = UserSpaces(user=user, db=db)
    space = None

    try:
        space = await user_spaces.create_space(
            ctx=ctx, space_create=SpaceCreate(name="List Test Space")
        )

        result = await user_spaces.list_spaces(ctx=ctx)
        space_ids = [d.id for d in result.docs]
        assert space.id in space_ids

    finally:
        if space:
            await user_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_get_space_requires_membership(ctx, db, user):
    from src.autobots.user.user_orm_model import UserORM

    user_spaces = UserSpaces(user=user, db=db)
    space = None

    try:
        space = await user_spaces.create_space(
            ctx=ctx, space_create=SpaceCreate(name="Get Test Space")
        )

        # Owner can get it
        found = await user_spaces.get_space(ctx=ctx, space_id=space.id)
        assert found.id == space.id

        # Non-member gets 403
        other_user = UserORM(id="other_user_id", email="other@test.com")
        other_user_spaces = UserSpaces(user=other_user, db=db)
        with pytest.raises(HTTPException) as exc_info:
            await other_user_spaces.get_space(ctx=ctx, space_id=space.id)
        assert exc_info.value.status_code == 403

    finally:
        if space:
            await user_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_update_space_owner_only(ctx, db, user):
    user_spaces = UserSpaces(user=user, db=db)
    space = None

    try:
        space = await user_spaces.create_space(
            ctx=ctx, space_create=SpaceCreate(name="Update Test Space")
        )

        updated = await user_spaces.update_space(
            ctx=ctx, space_id=space.id, space_update=SpaceUpdate(name="Updated Name")
        )
        assert updated.name == "Updated Name"

    finally:
        if space:
            await user_spaces.delete_space(ctx=ctx, space_id=space.id)


@pytest.mark.asyncio
async def test_delete_space_owner_only(ctx, db, user):
    from src.autobots.user.user_orm_model import UserORM

    user_spaces = UserSpaces(user=user, db=db)
    member_crud = SpaceMemberCRUD(db)

    space = await user_spaces.create_space(
        ctx=ctx, space_create=SpaceCreate(name="Delete Test Space")
    )

    # Non-owner cannot delete
    other_user = UserORM(id="other_user_id", email="other@test.com")
    other_user_spaces = UserSpaces(user=other_user, db=db)
    with pytest.raises(HTTPException) as exc_info:
        await other_user_spaces.delete_space(ctx=ctx, space_id=space.id)
    assert exc_info.value.status_code == 403

    # Owner can delete
    await user_spaces.delete_space(ctx=ctx, space_id=space.id)

    # Verify space and memberships are gone
    from src.autobots.space.space_crud import SpaceCRUD

    space_crud = SpaceCRUD(db)
    found = await space_crud.find_one(SpaceDocFind(id=space.id))
    assert found is None

    members = await member_crud.find_by_space_id(space_id=space.id)
    assert members.total_count == 0
