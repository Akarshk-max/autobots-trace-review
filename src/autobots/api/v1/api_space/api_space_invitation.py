import supabase_auth
from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from starlette.requests import Request

from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.space.space_auth import require_admin_or_owner
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.space.space_invitation_doc_model import (
    SpaceInvitationCreate,
    SpaceInvitationDoc,
    SpaceInvitationDocsFound,
)
from src.autobots.space.user_space_invitation import UserSpaceInvitations
from src.autobots.user.user_orm_model import UserORM

# Space-scoped invitation routes
router = APIRouter(prefix="/spaces", tags=["/spaces"])


@router.post("/{space_id}/invitations")
async def send_invitation(
    request: Request,
    space_id: str,
    invitation_create: SpaceInvitationCreate,
    caller_member: SpaceMemberDoc = Depends(require_admin_or_owner),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> SpaceInvitationDoc:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump())
    return await UserSpaceInvitations(user=user_orm, db=db).send_invitation(
        ctx=ctx,
        space_id=space_id,
        invitation_create=invitation_create,
        caller_membership=caller_member,
    )


@router.get("/{space_id}/invitations")
async def list_space_invitations(
    request: Request,
    space_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    _caller_member: SpaceMemberDoc = Depends(require_admin_or_owner),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> SpaceInvitationDocsFound:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump())
    return await UserSpaceInvitations(user=user_orm, db=db).list_space_invitations(
        ctx=ctx, space_id=space_id, limit=limit, offset=offset
    )


@router.delete("/{space_id}/invitations/{invitation_id}")
async def revoke_invitation(
    request: Request,
    space_id: str,
    invitation_id: str,
    caller_member: SpaceMemberDoc = Depends(require_admin_or_owner),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> None:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump())
    await UserSpaceInvitations(user=user_orm, db=db).revoke_invitation(
        ctx=ctx,
        space_id=space_id,
        invitation_id=invitation_id,
        caller_membership=caller_member,
    )


# User-scoped invitation routes (not space-prefixed)
invitations_router = APIRouter(prefix="/invitations", tags=["/invitations"])


@invitations_router.get("/")
async def list_my_invitations(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> SpaceInvitationDocsFound:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump())
    return await UserSpaceInvitations(user=user_orm, db=db).list_my_invitations(
        ctx=ctx, limit=limit, offset=offset
    )


@invitations_router.post("/{invitation_id}/accept")
async def accept_invitation(
    request: Request,
    invitation_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> SpaceMemberDoc:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump())
    return await UserSpaceInvitations(user=user_orm, db=db).accept_invitation(
        ctx=ctx, invitation_id=invitation_id
    )


@invitations_router.post("/{invitation_id}/decline")
async def decline_invitation(
    request: Request,
    invitation_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> None:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump())
    await UserSpaceInvitations(user=user_orm, db=db).decline_invitation(
        ctx=ctx, invitation_id=invitation_id
    )
