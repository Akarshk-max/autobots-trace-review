
import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from starlette import status
from starlette.requests import Request

from src.autobots import SettingsProvider
from src.autobots.access.access_doc_model import (
    AccessDoc,
    AccessDocsFound,
    AccessFind,
    AccessStatus,
    AccessSubscription,
)
from src.autobots.access.user_access import UserAccess
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_ACCESS,
    tags=[SettingsProvider.sget().API_ACCESS],
)


# @router.post("/")
# async def create_access(
#     request: Request,
#     access_create: AccessCreate,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> AccessDoc:
#     ctx = request.state.context
#     user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
#     doc = await UserAccess(user=user_orm, db=db).create_user_access(
#         ctx=ctx, access_create=access_create
#     )
#     return doc

@router.get("")
async def list_access(
    request: Request,
    subscription: AccessSubscription | None = None,
    status_: AccessStatus | None = None,
    limit: int = 100,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> AccessDocsFound:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    find = AccessFind(subscription=subscription, status=status_)
    return await UserAccess(user=user_orm, db=db).list_user_access(
        ctx=ctx,
        access_find=find,
        limit=limit,
        offset=offset,
    )

@router.get("/by_subscription/{subscription}")
async def get_access_by_subscription(
    request: Request,
    subscription: AccessSubscription,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> AccessDoc:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    doc = await UserAccess(user=user_orm, db=db).get_user_access_by_subscription(
        ctx=ctx, subscription=subscription
    )
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User access for subscription {subscription} not found",
        )
    return doc


@router.get("/{access_id}")
async def get_access(
    request: Request,
    access_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> AccessDoc:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    doc = await UserAccess(user=user_orm, db=db).get_user_access(ctx=ctx, access_id=access_id)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User access with id {access_id} not found",
        )
    return doc



# @router.put("/{access_id}")
# async def update_access(
#     request: Request,
#     access_id: str,
#     access_update: AccessUpdate,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> AccessDoc:
#     ctx = request.state.context
#     user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
#     return await UserAccess(user=user_orm, db=db).update_user_access(
#         ctx=ctx, access_id=access_id, update=access_update
#     )


# @router.put("/by_subscription/{subscription}")
# async def update_access_by_subscription(
#     request: Request,
#     subscription: AccessSubscription,
#     access_update: AccessUpdate,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> AccessDoc:
#     ctx = request.state.context
#     user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
#     return await UserAccess(user=user_orm, db=db).update_user_access_by_subscription(
#         ctx=ctx, subscription=subscription, update=access_update
#     )


# @router.delete("/{access_id}")
# async def delete_access(
#     request: Request,
#     access_id: str,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> AccessDoc:
#     ctx = request.state.context
#     user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)

#     access_doc = await UserAccess(user=user_orm, db=db).get_user_access(ctx=ctx, access_id=access_id)
#     if access_doc is None:
#         raise HTTPException(400, f"User access with {access_id} not found")

#     deleted_count = await UserAccess(user=user_orm, db=db).delete_user_access(ctx=ctx, access_id=access_id)
#     if deleted_count != 1:
#         raise HTTPException(500, "Error deleting user access")

#     return access_doc


# @router.delete("/by_subscription/{subscription}")
# async def delete_access_by_subscription(
#     request: Request,
#     subscription: AccessSubscription,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> int:
#     ctx = request.state.context
#     user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
#     deleted_count = await UserAccess(user=user_orm, db=db).delete_user_access_by_subscription(
#         ctx=ctx, subscription=subscription
#     )
#     return deleted_count
