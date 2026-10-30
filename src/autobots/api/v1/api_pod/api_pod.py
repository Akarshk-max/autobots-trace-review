import supabase_auth
from fastapi import APIRouter, Depends, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots import SettingsProvider
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.pod.pod_doc_model import PodDoc, PodUpdate
from src.autobots.pod.user_pod import UserPod
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_POD,
    tags=[SettingsProvider.sget().API_POD],
)


@router.get("/")
async def get_pod(
    request: Request,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> PodDoc:
    """Never 404s - a user without a pod gets an empty one created."""
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    return await UserPod(user_orm, db).get(ctx)


@router.put("/")
async def update_pod(
    request: Request,
    pod_update: PodUpdate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> PodDoc:
    """Partial: omitted fields keep their current value."""
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    return await UserPod(user_orm, db).update(ctx, pod_update)
