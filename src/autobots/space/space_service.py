from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.data_model.context import Context
from src.autobots.space.space_crud import SpaceMemberCRUD
from src.autobots.space.space_doc_model import (
    SpaceCreate,
    SpaceDoc,
    SpaceDocsFound,
    SpaceMemberDoc,
)
from src.autobots.space.user_space import UserSpaces
from src.autobots.user.user_orm_model import UserORM


class SpaceService:
    """Public contract for other modules to interact with Spaces."""

    @staticmethod
    async def create_space(
        ctx: Context, user: UserORM, db: AsyncIOMotorDatabase, space_create: SpaceCreate
    ) -> SpaceDoc:
        return await UserSpaces(user=user, db=db).create_space(ctx=ctx, space_create=space_create)

    @staticmethod
    async def list_spaces(
        ctx: Context, user: UserORM, db: AsyncIOMotorDatabase,
        limit: int = 100, offset: int = 0
    ) -> SpaceDocsFound:
        return await UserSpaces(user=user, db=db).list_spaces(
            ctx=ctx, limit=limit, offset=offset
        )

    @staticmethod
    async def get_space(
        ctx: Context, user: UserORM, db: AsyncIOMotorDatabase, space_id: str
    ) -> SpaceDoc:
        return await UserSpaces(user=user, db=db).get_space(ctx=ctx, space_id=space_id)

    @staticmethod
    async def get_membership(
        user: UserORM, db: AsyncIOMotorDatabase, space_id: str
    ) -> SpaceMemberDoc | None:
        return await SpaceMemberCRUD(db).find_membership(
            space_id=space_id, user_id=str(user.id)
        )
