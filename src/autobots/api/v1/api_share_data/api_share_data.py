import supabase_auth
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots import SettingsProvider
from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action_graph.action_graph.action_graph_doc_model import ActionGraphDoc
from src.autobots.action_team.action_team.action_team_doc_model import ActionTeamDoc
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.share_data.share_data_doc_model import (
    RunnableType,
    ShareDataCreate,
    ShareDataDoc,
    ShareDataDocsPage,
    ShareDataRead,
    ShareDataUpdate,
)
from src.autobots.share_data.user_share_data import UserShareData
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_SHARE_DATA, tags=[SettingsProvider.sget().API_SHARE_DATA]
)


@router.post("/")
async def create(
    request: Request,
    share_data_create: ShareDataCreate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> ShareDataDoc:
    ctx = request.state.context
    user = UserORM(id=user_res.user.id)
    try:
        share_data_doc = await UserShareData.create(
            ctx=ctx, user=user, db=db, share_data_create=share_data_create
        )
        return share_data_doc
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Failed to create share data: {str(e)}")
        match e:
            case HTTPException():
                raise e
            case _:
                raise HTTPException(503, f"Failed to create share data: {str(e)}")


@router.get("/")
async def read(
    request: Request,
    id: str = None,
    data_id: str = None,
    runnable_type: RunnableType = None,
    limit: int = 100,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> ShareDataDocsPage:
    ctx = request.state.context
    user = UserORM(id=user_res.user.id)
    try:
        share_data_read = ShareDataRead(id=id, data_id=data_id, runnable_type=runnable_type)
        page = await UserShareData.read(
            ctx=ctx, user=user, db=db, share_data_read=share_data_read, limit=limit, offset=offset
        )
        return page
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Failed to read share data: {str(e)}")
        match e:
            case HTTPException():
                raise e
            case _:
                raise HTTPException(503, f"Failed to read share data: {str(e)}")


@router.get("/{id}")
async def get(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> ShareDataDoc:
    ctx = request.state.context
    user = UserORM(id=user_res.user.id)
    try:
        share_data_doc = await UserShareData.get(ctx=ctx, user=user, db=db, id=id)
        return share_data_doc
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Failed to get share data: {str(e)}")
        match e:
            case HTTPException():
                raise e
            case _:
                raise HTTPException(503, f"Failed to get share data: {str(e)}")


@router.put("/{id}")
async def update(
    request: Request,
    id: str,
    share_data_update: ShareDataUpdate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> ShareDataDoc:
    ctx = request.state.context
    user = UserORM(id=user_res.user.id)
    try:
        updated_share_data_doc = await UserShareData.update(
            ctx=ctx, user=user, db=db, id=id, share_data_update=share_data_update
        )
        return updated_share_data_doc
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Failed to update share data: {str(e)}")
        match e:
            case HTTPException():
                raise e
            case _:
                raise HTTPException(503, f"Failed to update share data: {str(e)}")


@router.delete("/{id}")
async def delete(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> int:
    ctx = request.state.context
    user = UserORM(id=user_res.user.id)
    try:
        deleted_count = await UserShareData.delete(ctx=ctx, user=user, db=db, share_data_id=id)
        return deleted_count
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Failed to delete share data: {str(e)}")
        match e:
            case HTTPException():
                raise e
            case _:
                raise HTTPException(503, f"Failed to delete share data: {str(e)}")


@router.post("/{id}/clone")
async def clone(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ActionDoc | ActionGraphDoc | ActionTeamDoc:
    ctx = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)
    try:
        cloned_data = await UserShareData.clone(ctx=ctx, user=user, db=db, share_data_id=id)
        return cloned_data
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Failed to clone shared data: {str(e)}")
        match e:
            case HTTPException():
                raise e
            case _:
                raise HTTPException(503, f"Failed to clone shared data: {str(e)}")
