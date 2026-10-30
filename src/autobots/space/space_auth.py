import supabase_auth
from fastapi import Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.space.space_crud import SpaceMemberCRUD
from src.autobots.space.space_doc_model import ADMIN_ROLES, SpaceMemberDoc, SpaceMemberRole
from src.autobots.user.user_orm_model import UserORM


async def get_optional_space_id(
    space_id: str | None = Query(default=None),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> str | None:
    """If space_id query param is provided, validates membership. Returns space_id or None."""
    if space_id:
        user_orm = UserORM(**user_res.user.model_dump())
        member = await SpaceMemberCRUD(db).find_membership(
            space_id=space_id, user_id=str(user_orm.id)
        )
        if not member:
            raise HTTPException(403, "Not a member of this space")
    return space_id


async def get_optional_space_member(
    space_id: str | None = Query(default=None),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> SpaceMemberDoc | None:
    """If space_id query param is provided, validates membership. Returns SpaceMemberDoc or None."""
    if not space_id:
        return None
    user_orm = UserORM(**user_res.user.model_dump())
    member = await SpaceMemberCRUD(db).find_membership(
        space_id=space_id, user_id=str(user_orm.id)
    )
    if not member:
        raise HTTPException(403, "Not a member of this space")
    return member


async def resolve_space_member(
    space_id: str | None, user: UserORM, db: AsyncIOMotorDatabase
) -> SpaceMemberDoc | None:
    """Resolve SpaceMemberDoc from space_id. Call directly from endpoint bodies."""
    if not space_id:
        return None
    member = await SpaceMemberCRUD(db).find_membership(
        space_id=space_id, user_id=str(user.id)
    )
    if not member:
        raise HTTPException(403, "Not a member of this space")
    return member


async def get_space_member(
    space_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> SpaceMemberDoc:
    """Validates user is a member of the space. Returns membership doc. 403 if not."""
    user_orm = UserORM(**user_res.user.model_dump())
    member = await SpaceMemberCRUD(db).find_membership(
        space_id=space_id, user_id=str(user_orm.id)
    )
    if not member:
        raise HTTPException(403, "Not a member of this space")
    return member


async def require_owner(
    member: SpaceMemberDoc = Depends(get_space_member),
) -> SpaceMemberDoc:
    """Asserts role == owner. 403 otherwise."""
    if member.role != SpaceMemberRole.owner:
        raise HTTPException(403, "Owner role required")
    return member


async def require_admin_or_owner(
    member: SpaceMemberDoc = Depends(get_space_member),
) -> SpaceMemberDoc:
    """Asserts role in (owner, admin). 403 otherwise."""
    if member.role not in (SpaceMemberRole.owner, SpaceMemberRole.admin):
        raise HTTPException(403, "Admin or owner role required")
    return member


def is_space_member_a_admin(space_member: SpaceMemberDoc | None,) -> bool:
    """Returns True if space_member has admin/owner role, False otherwise."""
    if space_member and space_member.id and space_member.role in ADMIN_ROLES:
        return True
    return False
    # return space_member is not None and space_member.role in ADMIN_ROLES

