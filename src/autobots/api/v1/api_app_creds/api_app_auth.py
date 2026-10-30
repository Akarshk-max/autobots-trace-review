import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from starlette.requests import Request

from src.autobots import SettingsProvider
from src.autobots.integrations.integration_model import (
    IntegrationLiteDocPage,
    IntegrationRead,
)
from src.autobots.integrations.user_integration import UserIntegration
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
    prefix=SettingsProvider.sget().API_APP_AUTH, tags=[SettingsProvider.sget().API_APP_AUTH]
)


@router.get("/", deprecated=True)
async def list_app_auth(
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
) -> IntegrationLiteDocPage:
    ctx = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)
    app_auth_find = IntegrationRead(id=id, app_name=app, name=name)
    integration_docs = await UserIntegration.list(
        ctx=ctx,
        db=db,
        user=user,
        integration_list=app_auth_find,
        limit=limit,
        offset=offset,
   )
    return integration_docs


@router.get("/sync", deprecated=True)
async def sync_app_auth(
    request: Request,
    name: str,
    app: AppTypes,
    # limit: int = 100, offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.AppConn, ResourceOperation.Read)),
) -> dict[str, str]:
    ctx = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)
    new_api_auth_doc = await UserIntegration.sync_with_creds(ctx=ctx, db=db, user=user, name=name, app_name=app, decrypt=True)
    if not new_api_auth_doc:
        raise HTTPException(status_code=400, detail="Error in App Auth sync")
    logger.bind(ctx=ctx, name=name, app_name=new_api_auth_doc.app_name).info("App Auth synced")
    return {"ok": "done"}


class Deleted(BaseModel):
    deleted_count: int


@router.delete("/{id}", deprecated=True)
async def delete_app_auth(
    request: Request,
    app_auth_delete: IntegrationRead,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.AppConn, ResourceOperation.Delete)),
) -> Deleted:
    ctx = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)
    deleted = await UserIntegration.delete(ctx=ctx, db=db, user=user, app_auth_delete=app_auth_delete, decrypt=True)
    if not deleted:
        raise HTTPException(status_code=404, detail="Could not delete app auth")
    return Deleted(deleted_count=deleted)


@router.get("/{name}", deprecated=True)
async def refresh_app_auth(
    request: Request,
    name: str,
    app: AppTypes,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.AppConn, ResourceOperation.Read)),
):
    ctx = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)
    # now app name is also mandatory
    new_app_auth_doc = await UserIntegration.refresh(ctx=ctx, db=db, user=user, name=name, app_name=app, decrypt=True)
    if not new_app_auth_doc:
        raise HTTPException(status_code=404, detail=f"App auth not found for name='{name}'")
    logger.bind(ctx=ctx, name=name, app_name=new_app_auth_doc.app_name).info("App Auth refreshed")
    return {"ok": "done"}
