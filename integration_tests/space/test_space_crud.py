import pytest

from src.autobots.space.space_crud import SpaceCRUD, SpaceMemberCRUD
from src.autobots.space.space_doc_model import (
    SpaceDocCreate,
    SpaceDocFind,
    SpaceMemberDocCreate,
    SpaceMemberRole,
)


@pytest.mark.asyncio
async def test_space_crud_create_and_find(ctx, db, user):
    space_crud = SpaceCRUD(db)
    member_crud = SpaceMemberCRUD(db)

    try:
        # Create a space
        space_doc_create = SpaceDocCreate(
            user_id=str(user.id),
            name="Test Space CRUD",
            description="A test space",
        )
        space = await space_crud.insert_one(space_doc_create)
        assert not isinstance(space, Exception)
        assert space.name == "Test Space CRUD"
        assert space.user_id == str(user.id)

        # Find the space
        found = await space_crud.find_one(SpaceDocFind(id=space.id))
        assert found is not None
        assert found.name == "Test Space CRUD"

        # Create a membership
        member_create = SpaceMemberDocCreate(
            user_id=str(user.id),
            space_id=space.id,
            role=SpaceMemberRole.owner,
            invited_by=str(user.id),
        )
        member = await member_crud.insert_one(member_create)
        assert not isinstance(member, Exception)
        assert member.role == SpaceMemberRole.owner

        # Find membership
        found_member = await member_crud.find_membership(
            space_id=space.id, user_id=str(user.id)
        )
        assert found_member is not None
        assert found_member.role == SpaceMemberRole.owner

        # Find by user_id
        user_memberships = await member_crud.find_by_user_id(user_id=str(user.id))
        assert user_memberships.total_count >= 1

        # Find by space_id
        space_members = await member_crud.find_by_space_id(space_id=space.id)
        assert space_members.total_count == 1

        # Count owners
        owner_count = await member_crud.count_owners(space_id=space.id)
        assert owner_count == 1

    finally:
        # Cleanup
        await member_crud.delete_by_space_id(space.id)
        await space_crud.delete_many(SpaceDocFind(id=space.id, user_id=str(user.id)))


@pytest.mark.asyncio
async def test_space_crud_find_by_ids(ctx, db, user):
    space_crud = SpaceCRUD(db)

    try:
        # Create two spaces
        space1 = await space_crud.insert_one(
            SpaceDocCreate(user_id=str(user.id), name="Space One")
        )
        space2 = await space_crud.insert_one(
            SpaceDocCreate(user_id=str(user.id), name="Space Two")
        )
        assert not isinstance(space1, Exception)
        assert not isinstance(space2, Exception)

        # Find by IDs
        result = await space_crud.find_by_ids([space1.id, space2.id])
        assert result.total_count == 2
        names = {d.name for d in result.docs}
        assert "Space One" in names
        assert "Space Two" in names

    finally:
        await space_crud.delete_many(SpaceDocFind(id=space1.id, user_id=str(user.id)))
        await space_crud.delete_many(SpaceDocFind(id=space2.id, user_id=str(user.id)))
