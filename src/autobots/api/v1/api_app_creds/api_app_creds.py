from typing import Any, List

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from starlette.requests import Request

from src.autobots import SettingsProvider
from src.autobots.integrations.app_creds.app_creds_model import (
    AppCredsCreate,
    AppCredsDoc,
    AppCredsLiteDocPage,
    AppCredsRead,
    AppCredsUpdate,
)
from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_types import AppTypes
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.product.product_model import (
    ProductResource,
    ResourceOperation,
    require_permission,
)
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_APP_CREDS, tags=[SettingsProvider.sget().API_APP_CREDS]
)


@router.get("/types", deprecated=True)
async def get_types(
    request: Request,
    user: UserORM = Depends(get_offline_user_from_access_token),
    _perm=Depends(require_permission(ProductResource.AppConn, ResourceOperation.Read)),
) -> List[str]:
    ctx = request.state.context
    types = await UserAppCreds.app_creds_types(ctx)
    return types


@router.get("/types/{app}", deprecated=True)
async def get_creds_types(
    request: Request,
    app: AppTypes,
    user: UserORM = Depends(get_offline_user_from_access_token),
    _perm=Depends(require_permission(ProductResource.AppConn, ResourceOperation.Read)),
) -> dict[str, Any]:
    ctx = request.state.context
    model_json_schema = await UserAppCreds.app_creds_data_type(ctx=ctx, app_name=app)
    return model_json_schema


@router.post("/", deprecated=True)
async def create_app_creds(
    request: Request,
    app_creds_create: AppCredsCreate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.AppConn, ResourceOperation.Create)),
) -> AppCredsDoc:
    ctx = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)
    app_cred_doc = await UserAppCreds.create_app_creds(
        ctx=ctx,
        db=db,
        user=user,
        app_creds_create=app_creds_create,
   )
    if not app_cred_doc:
        raise HTTPException(status_code=400, detail="Could not create app creds")
    return app_cred_doc.model_copy(update={"creds": {}})


@router.get("/", deprecated=True)
async def list_app_creds(
    request: Request,
    id: str = None,
    app: AppTypes = None,
    name: str = None,
    limit: int = 100,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.AppConn, ResourceOperation.Read)),
) -> AppCredsLiteDocPage:
    ctx = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)
    app_creds_find = AppCredsRead(id=id, app_name=app, name=name)
    app_creds_docs = await UserAppCreds.list_app_creds(
        ctx=ctx,
        db=db,
        user=user,
        app_creds_list=app_creds_find,
        limit=limit,
        offset=offset,
   )
    return app_creds_docs


@router.get("/{id}", deprecated=True)
async def get_app_creds(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.AppConn, ResourceOperation.Read)),
) -> AppCredsDoc:
    ctx = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)
    app_creds_doc = await UserAppCreds.get_app_creds(
        ctx=ctx, db=db, user=user, app_creds_id=id , decrypt=False
    )
    if not app_creds_doc:
        raise HTTPException(status_code=404, detail="Could not find app creds")
    return app_creds_doc.model_copy(update={"creds": {}})

@router.put("/{id}", deprecated=True)
async def update_app_creds(
    request: Request,
    id: str,
    app_creds_update: AppCredsUpdate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.AppConn, ResourceOperation.Update)),
) -> AppCredsDoc:

    ctx = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)
    # app_creds = AppCredsUpdate(**app_creds_update.model_dump(exclude_none=True))
    app_creds_doc = await UserAppCreds.update_app_creds(
        ctx=ctx,
        db=db,
        user=user,
        id=id,
        app_creds_update=app_creds_update,
   )
    if not app_creds_doc:
        raise HTTPException(status_code=404, detail="Could not update app creds")
    return app_creds_doc.model_copy(update={"creds": {}})



class Deleted(BaseModel):
    deleted_count: int


@router.delete("/{id}", deprecated=True)
async def delete_app_creds(
    request: Request,
    app_creds_delete: AppCredsRead,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.AppConn, ResourceOperation.Delete)),
) -> Deleted:
    ctx = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)
    deleted = await UserAppCreds.delete_app_creds(
        ctx=ctx,
        db=db,
        user=user,
        app_creds_delete=app_creds_delete,
   )
    if not deleted:
        raise HTTPException(status_code=404, detail="Could not delete app creds")
    return Deleted(deleted_count=deleted)
