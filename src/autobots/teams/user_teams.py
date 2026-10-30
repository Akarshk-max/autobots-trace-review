from http import HTTPStatus

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.results import DeleteResult

from src.autobots.data_model.context import Context
from src.autobots.teams.team_crud import TeamCRUD
from src.autobots.teams.team_doc_model import (
    TeamCreate,
    TeamDoc,
    TeamDocCreate,
    TeamDocRead,
    TeamDocUpdate,
    TeamLiteDocPage,
    TeamRead,
    TeamUpdate,
)
from src.autobots.user.user_orm_model import UserORM


class UserTeams:
    @staticmethod
    async def create(
        ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, team_create: TeamCreate
    ) -> TeamDoc | None:
        crud = TeamCRUD(db=db)
        team_doc = None
        team_doc = await UserTeams.get_by_name(ctx=ctx, db=db, user=user, name=team_create.name)
        if team_doc:
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT, detail="There is already a team by this name"
            )
        assert team_doc is None
        create_doc = TeamDocCreate(
            user_id=str(user.id), **team_create.model_dump(exclude_none=True)
        )
        doc = await crud.insert_one(create_doc)
        match doc:
            case Exception():
                logger.bind(ctx=ctx, error=str(doc)).exception("Cannot create team")
            case _:
                team_doc = TeamDoc.model_validate(doc)
                logger.bind(ctx=ctx).info(f"Created team: {team_create.name}")
        return team_doc

    @staticmethod
    async def list(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        team_read: TeamRead,
        limit: int = 100,
        offset: int = 0,
    ) -> TeamLiteDocPage:
        crud = TeamCRUD(db=db)
        find_doc = TeamDocRead(user_id=str(user.id), **team_read.model_dump(exclude_none=True))
        page = await crud.find_page(doc_find=find_doc, limit=limit, offset=offset)
        team_page = TeamLiteDocPage.model_validate(page.model_dump(exclude_none=True))
        return team_page

    @staticmethod
    async def get(
        ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, team_id: str
    ) -> TeamDoc | None:
        crud = TeamCRUD(db=db)
        doc_find = TeamDocRead(user_id=str(user.id), id=team_id)
        doc = await crud.find_one(doc_find=doc_find)
        return TeamDoc.model_validate(doc) if doc else None

    @staticmethod
    async def get_by_name(
        ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, name: str
    ) -> TeamDoc | None:
        crud = TeamCRUD(db=db)
        doc_find = TeamDocRead(user_id=str(user.id), name=name)
        doc = await crud.find_one(doc_find=doc_find)
        return TeamDoc.model_validate(doc) if doc else None

    @staticmethod
    async def update(
        ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, id: str, team_update: TeamUpdate
    ) -> TeamDoc | None:
        crud = TeamCRUD(db=db)
        doc_update = TeamDocUpdate(
            id=id, user_id=str(user.id), **team_update.model_dump(exclude_none=True)
        )
        doc = await crud.update_one(doc_update=doc_update)
        team_doc = None
        match doc:
            case Exception():
                logger.bind(ctx=ctx, error=str(doc)).exception("Cannot update team")
            case TeamDoc():
                team_doc = doc
        return team_doc

    @staticmethod
    async def delete(
        ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, team_delete: TeamRead
    ) -> int | None:
        crud = TeamCRUD(db=db)
        find_doc = TeamDocRead(user_id=str(user.id), **team_delete.model_dump(exclude_none=True))
        deleted = await crud.delete_many(doc_find=find_doc)
        match deleted:
            case DeleteResult():
                return deleted.deleted_count
            case Exception():
                logger.bind(ctx=ctx, error=str(deleted)).error("Cannot delete teams")
        return None
