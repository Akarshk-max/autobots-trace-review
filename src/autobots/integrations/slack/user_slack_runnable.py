from http import HTTPStatus

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.results import DeleteResult

from src.autobots.data_model.context import Context
from src.autobots.integrations.slack.slack_runnable_crud import SlackRunnableCRUD
from src.autobots.integrations.slack.slack_runnable_doc_model import (
    SlackRunnableCreate,
    SlackRunnableDoc,
    SlackRunnableDocCreate,
    SlackRunnableDocRead,
    SlackRunnableDocUpdate,
    SlackRunnableLiteDocPage,
    SlackRunnableRead,
    SlackRunnableUpdate,
)
from src.autobots.user.user_orm_model import UserORM


class UserSlackRunnable:
    @staticmethod
    async def create(
        ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, create: SlackRunnableCreate
    ) -> SlackRunnableDoc | None:
        crud = SlackRunnableCRUD(db=db)
        slack_runnable_doc = None
        slack_runnable_doc = await UserSlackRunnable.get_by_slack_thread_id(
            ctx=ctx, db=db, user=user, slack_thread_id=create.slack_thread_id
        )
        if slack_runnable_doc:
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=f"There is already a SlackRunnable for this thrad id {create.slack_thread_id}",
            )
        assert slack_runnable_doc is None
        create_doc = SlackRunnableDocCreate(
            user_id=str(user.id), **create.model_dump(exclude_none=True)
        )
        doc = await crud.insert_one(create_doc)
        match doc:
            case Exception():
                logger.bind(ctx=ctx, error=str(doc)).exception("Cannot create SlackRunnable")
            case _:
                slack_runnable_doc = SlackRunnableDoc.model_validate(doc)
                logger.bind(ctx=ctx, **slack_runnable_doc.model_dump()).info(
                    "Created SlackRunnable"
                )
        return slack_runnable_doc

    @staticmethod
    async def list(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        read: SlackRunnableRead,
        limit: int = 100,
        offset: int = 0,
    ) -> SlackRunnableLiteDocPage:
        crud = SlackRunnableCRUD(db=db)
        find_doc = SlackRunnableDocRead(user_id=str(user.id), **read.model_dump(exclude_none=True))
        page = await crud.find_page(doc_find=find_doc, limit=limit, offset=offset)
        slack_runnable_page = SlackRunnableLiteDocPage.model_validate(
            page.model_dump(exclude_none=True)
        )
        return slack_runnable_page

    @staticmethod
    async def get(
        ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, id: str
    ) -> SlackRunnableDoc | None:
        crud = SlackRunnableCRUD(db=db)
        doc_find = SlackRunnableDocRead(user_id=str(user.id), id=id)
        doc = await crud.find_one(doc_find=doc_find)
        return SlackRunnableDoc.model_validate(doc) if doc else None

    @staticmethod
    async def get_by_slack_thread_id(
        ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, slack_thread_id: str
    ) -> SlackRunnableDoc | None:
        crud = SlackRunnableCRUD(db=db)
        doc_find = SlackRunnableDocRead(user_id=str(user.id), slack_thread_id=slack_thread_id)
        doc = await crud.find_one(doc_find=doc_find)
        return SlackRunnableDoc.model_validate(doc) if doc else None

    @staticmethod
    async def update(
        ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, id: str, update: SlackRunnableUpdate
    ) -> SlackRunnableDoc | None:
        crud = SlackRunnableCRUD(db=db)
        doc_update = SlackRunnableDocUpdate(
            id=id, user_id=str(user.id), **update.model_dump(exclude_none=True)
        )
        doc = await crud.update_one(doc_update=doc_update)
        slack_runnable_doc = None
        match doc:
            case Exception():
                logger.bind(ctx=ctx, error=str(doc)).exception("Cannot update SlackRunnable")
            case SlackRunnableDoc():
                slack_runnable_doc = doc
        return slack_runnable_doc

    @staticmethod
    async def delete(
        ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, delete: SlackRunnableRead
    ) -> int | None:
        crud = SlackRunnableCRUD(db=db)
        find_doc = SlackRunnableDocRead(
            user_id=str(user.id), **delete.model_dump(exclude_none=True)
        )
        deleted = await crud.delete_many(doc_find=find_doc)
        match deleted:
            case DeleteResult():
                return deleted.deleted_count
            case Exception():
                logger.bind(ctx=ctx, error=str(deleted)).error("Cannot delete SlackRunnable")
        return None
