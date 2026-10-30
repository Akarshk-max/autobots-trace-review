from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.data_model.context import Context
from src.autobots.space.space_service import SpaceService
from src.autobots.user.user import User
from src.autobots.user.user_orm_model import UserORM


async def resolve_run_user(
    ctx: Context,
    db: AsyncIOMotorDatabase,
    user_id: str,
    space_id: str | None,
    access_token: str | None = None,
) -> UserORM:
    user_doc = await User.get(ctx=ctx, user=UserORM(id=user_id), id=user_id, db=db)
    user = UserORM.model_validate(user_doc.model_dump())
    user.access_token = access_token

    if space_id is None:
        return user

    space_member = await SpaceService.get_membership(user=user, db=db, space_id=space_id)
    if space_member is None:
        raise HTTPException(403, f"User {user_id} is not a member of space {space_id}")
    user.space_member = space_member
    return user
