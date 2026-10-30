from typing import List
from src.autobots.data_model.app_state import AppState
import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from starlette.requests import Request

from src.autobots import SettingsProvider
from src.autobots.auth.security import (
    get_offline_user_from_access_token,
    get_online_admin_user_from_access_token,
)
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.product.product_model import (
    ProductResource,
    ResourceOperation,
    require_permission,
)
from src.autobots.schedule.admin_schedule import AdminSchedule
from src.autobots.schedule.schedule_doc_model import (
    ScheduleCreate,
    ScheduleDoc,
    ScheduleFind,
    ScheduleUpdate,
)
from src.autobots.schedule.user_schedule import UserSchedule
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_SCHEDULES, tags=[SettingsProvider.sget().API_SCHEDULES]
)


@router.post("/")
async def create_schedule(
    request: Request,
    schedule_create: ScheduleCreate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Schedules, ResourceOperation.Create)),
) -> ScheduleDoc:
    ctx = request.state.context
    app_state: AppState = request.state.app_state
    try:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        resp = await UserSchedule(user=user_orm, db=db).create_user_schedule(
            ctx=ctx, app_state=app_state, schedule_create=schedule_create
        )
        return resp
    except Exception as e:
        logger.bind(ctx=ctx).exception(str(e))
        raise (
            e if isinstance(e, HTTPException) else HTTPException(500, "Error in creating schedule")
        )


@router.get("/")
async def list_schedules(
    request: Request,
    id: str = None,
    name: str = None,
    runnable_id: str = None,
    limit: int = 100,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Schedules, ResourceOperation.Read)),
) -> List[ScheduleDoc]:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    find = ScheduleFind(id=id, name=name, runnable_id=runnable_id)
    action_docs = await UserSchedule(user=user_orm, db=db).list_user_schedule(
        ctx, find, limit, offset
    )
    return action_docs


@router.get("/{id}")
async def get_schedule(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Schedules, ResourceOperation.Read)),
) -> ScheduleDoc:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    action_doc = await UserSchedule(user=user_orm, db=db).get_user_schedule(ctx=ctx, schedule_id=id)
    return action_doc


@router.put("/{id}")
async def update_schedule(
    request: Request,
    id: str,
    schedule_update: ScheduleUpdate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Schedules, ResourceOperation.Update)),
) -> ScheduleDoc:
    ctx = request.state.context
    app_state: AppState  = request.state.app_state
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    action_doc = await UserSchedule(user=user_orm, db=db).update_user_schedule(
        ctx=ctx, app_state=app_state, schedule_id=id, schedule_update=schedule_update
    )
    return action_doc


@router.delete("/{id}")
async def delete_schedule(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Schedules, ResourceOperation.Delete)),
) -> ScheduleDoc:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_schedule = UserSchedule(user=user_orm, db=db)
    schedule_doc = await user_schedule.get_user_schedule(ctx=ctx, schedule_id=id)
    if schedule_doc is None:
        raise HTTPException(400, "Schedule not found")
    logger.bind(
        ctx=ctx,
        schedule_doc_id=schedule_doc.id,
        schedule_name=schedule_doc.name,
        schedule_doc_job_id=schedule_doc.job_id,
    ).info(f"Deleting schedule: {id}")
    deleted_count = await user_schedule.delete_user_schedule(ctx=ctx, schedule_id=id)
    if deleted_count != 1:
        raise HTTPException(500, "Error in deleting schedule")
    return schedule_doc


@router.put("/all/once")
async def update_all_schedule(
    request: Request,
    user_res: supabase_auth.UserResponse = Depends(get_online_admin_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Schedules, ResourceOperation.Update)),
) -> List[ScheduleDoc]:
    ctx = request.state.context
    app_state: AppState = request.state.app_state
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    logger.bind(ctx=ctx, user_id=user_orm.id).info("Updating all schedules")
    admin_schedule = AdminSchedule(db=db)
    docs = await admin_schedule.re_update_schedules(ctx=ctx , app_state=app_state)
    return docs
