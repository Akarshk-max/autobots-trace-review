import supabase_auth
from fastapi import APIRouter, Depends, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots import SettingsProvider
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.batch.batch_doc_model import BatchDocsFound, BatchFind, BatchResponse
from src.autobots.batch.space_batches import SpaceBatches
from src.autobots.batch.user_batch import UserBatch
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.event_result.event_result_model import EventType
from src.autobots.space.space_auth import get_optional_space_member, is_space_member_a_admin
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_BATCHES,
    tags=[SettingsProvider.sget().API_BATCHES],
)


@router.get("/")
async def list_batches(
    request: Request,
    id: str = None,
    runnable_type: EventType = None,
    runnable_id: str = None,
    limit: int = 20,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> BatchDocsFound:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    batch_find = BatchFind(id=id, runnable_type=runnable_type, runnable_id=runnable_id)
    if is_space_admin:
        return await SpaceBatches(space_member=space_member, db=db).list_batches(
            ctx, batch_find, limit, offset
        )
    user_orm = UserORM(id=user_res.user.id, space_member=space_member)
    return await UserBatch(user_orm, db).list_batches(ctx, batch_find, limit, offset)


@router.get("/{batch_id}")
async def get_batch(
    request: Request,
    batch_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> BatchResponse:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    if is_space_admin:
        batch = await SpaceBatches(space_member=space_member, db=db).get_batch(ctx, batch_id)
    else:
        user_orm = UserORM(id=user_res.user.id, space_member=space_member)
        batch = await UserBatch(user_orm, db).get_batch(ctx, batch_id)
    if batch is None:
        # Runs created before Batches existed carry a batch_id with no parent, so
        # an unknown id is an ordinary miss rather than an error.
        raise HTTPException(status_code=404, detail="Batch not found")
    return batch
