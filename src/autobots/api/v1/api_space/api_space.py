import supabase_auth
from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from starlette.requests import Request

from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.space.space_auth import get_space_member, require_admin_or_owner, require_owner
from src.autobots.space.space_doc_model import (
    SpaceCreate,
    SpaceDoc,
    SpaceDocsFound,
    SpaceMemberDoc,
    SpaceMemberDocsFound,
    SpaceMemberRole,
    SpaceMemberUpdate,
    SpaceUpdate,
)
from src.autobots.space.user_space import UserSpaces
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(prefix="/spaces", tags=["/spaces"])


@router.post("/")
async def create_space(
    request: Request,
    space_create: SpaceCreate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> SpaceDoc:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump())
    return await UserSpaces(user=user_orm, db=db).create_space(ctx=ctx, space_create=space_create)


@router.get("/")
async def list_spaces(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> SpaceDocsFound:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump())
    return await UserSpaces(user=user_orm, db=db).list_spaces(ctx=ctx, limit=limit, offset=offset)


@router.get("/{space_id}")
async def get_space(
    request: Request,
    space_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> SpaceDoc:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump())
    return await UserSpaces(user=user_orm, db=db).get_space(ctx=ctx, space_id=space_id)


@router.put("/{space_id}")
async def update_space(
    request: Request,
    space_id: str,
    space_update: SpaceUpdate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> SpaceDoc:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump())
    return await UserSpaces(user=user_orm, db=db).update_space(
        ctx=ctx, space_id=space_id, space_update=space_update
    )


@router.delete("/{space_id}")
async def delete_space(
    request: Request,
    space_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> None:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump())
    await UserSpaces(user=user_orm, db=db).delete_space(ctx=ctx, space_id=space_id)


# --- Membership Management ---


class AddMemberRequest(BaseModel):
    user_id: str
    role: SpaceMemberRole = SpaceMemberRole.member


@router.post("/{space_id}/members")
async def add_member(
    request: Request,
    space_id: str,
    body: AddMemberRequest,
    caller_member: SpaceMemberDoc = Depends(require_admin_or_owner),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> SpaceMemberDoc:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump())
    return await UserSpaces(user=user_orm, db=db).add_member(
        ctx=ctx,
        space_id=space_id,
        target_user_id=body.user_id,
        role=body.role,
        caller_membership=caller_member,
    )


@router.get("/{space_id}/members")
async def list_members(
    request: Request,
    space_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    _member: SpaceMemberDoc = Depends(get_space_member),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> SpaceMemberDocsFound:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump())
    return await UserSpaces(user=user_orm, db=db).list_members(
        ctx=ctx, space_id=space_id, limit=limit, offset=offset
    )


@router.put("/{space_id}/members/{target_user_id}")
async def update_member_role(
    request: Request,
    space_id: str,
    target_user_id: str,
    role_update: SpaceMemberUpdate,
    caller_member: SpaceMemberDoc = Depends(require_owner),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> SpaceMemberDoc:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump())
    return await UserSpaces(user=user_orm, db=db).update_member_role(
        ctx=ctx,
        space_id=space_id,
        target_user_id=target_user_id,
        role_update=role_update,
        caller_membership=caller_member,
    )


@router.delete("/{space_id}/members/{target_user_id}")
async def remove_member(
    request: Request,
    space_id: str,
    target_user_id: str,
    caller_member: SpaceMemberDoc = Depends(get_space_member),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> None:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump())
    await UserSpaces(user=user_orm, db=db).remove_member(
        ctx=ctx,
        space_id=space_id,
        target_user_id=target_user_id,
        caller_membership=caller_member,
    )
