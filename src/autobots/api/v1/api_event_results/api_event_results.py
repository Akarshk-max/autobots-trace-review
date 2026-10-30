import supabase_auth
from fastapi import APIRouter, Depends, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots import SettingsProvider
from src.autobots.auth.security import get_online_admin_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.event_result.admin_event_result import AdminEventResult
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_EVENT_RESULTS,
    tags=[SettingsProvider.sget().API_EVENT_RESULTS],
)


@router.get("/{id}")
async def get_event_result(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_online_admin_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    ctx = request.state.context
    user = UserORM(**user_res.user.model_dump())
    if not SettingsProvider.is_system_email(user.email):
        raise HTTPException(404, "Not found")
    admin_event_result = AdminEventResult(user=user, db=db)
    event_result_doc = await admin_event_result.get_event_result_by_id(ctx=ctx, id=id)
    if not event_result_doc:
        raise HTTPException(404, "Event result not found")
    return event_result_doc
