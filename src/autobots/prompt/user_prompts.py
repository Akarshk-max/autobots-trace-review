from http import HTTPStatus

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.results import DeleteResult

from src.autobots.data_model.context import Context
from src.autobots.prompt.prompt_crud import PromptCRUD
from src.autobots.prompt.prompt_doc_model import (
    PromptCreate,
    PromptDoc,
    PromptDocCreate,
    PromptDocRead,
    PromptDocUpdate,
    PromptLiteDocPage,
    PromptRead,
    PromptUpdate,
    PublishedPromptDocRead,
)
from src.autobots.user.user_orm_model import UserORM


class UserPrompt:
    @staticmethod
    async def create(ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, prompt_create: PromptCreate) -> PromptDoc:
        crud = PromptCRUD(db=db)

        existing = await UserPrompt.get_by_name(ctx=ctx, db=db, user=user, name=prompt_create.name)
        if existing:
            raise HTTPException(HTTPStatus.CONFLICT, "There is already a prompt by this name")

        prompt_create.is_published = True  # same behavior as Character

        create_doc = PromptDocCreate(user_id=str(user.id), **prompt_create.model_dump(exclude_none=True))
        doc = await crud.insert_one(create_doc)

        match doc:
            case Exception():
                logger.bind(ctx=ctx, error=str(doc)).exception("Cannot create prompt")
                raise HTTPException(500, "Failed to create prompt")
            case _:
                return PromptDoc.model_validate(doc)

    @staticmethod
    async def list(ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, prompt_read: PromptRead, limit: int = 100, offset: int = 0) -> PromptLiteDocPage:
        crud = PromptCRUD(db=db)
        find_doc_read = PromptDocRead(user_id=str(user.id), **prompt_read.model_dump(exclude_none=True))
        page = await crud.find_page(doc_find=find_doc_read, limit=limit, offset=offset)
        return PromptLiteDocPage.model_validate(page.model_dump(exclude_none=True))

    @staticmethod
    async def get(ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, prompt_id: str) -> PromptDoc | None:
        crud = PromptCRUD(db=db)
        doc_find_read = PromptDocRead(user_id=str(user.id), id=prompt_id)
        doc_find_published_read = PublishedPromptDocRead(id=prompt_id)
        doc = await crud.find_one(doc_find=doc_find_read, or_find_queries=[doc_find_published_read])
        return PromptDoc.model_validate(doc) if doc else None

    @staticmethod
    async def get_by_name(ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, name: str) -> PromptDoc | None:
        crud = PromptCRUD(db=db)
        doc_find_read = PromptDocRead(user_id=str(user.id), name=name)
        doc = await crud.find_one(doc_find=doc_find_read)
        return PromptDoc.model_validate(doc) if doc else None

    @staticmethod
    async def update(ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, id: str, prompt_update: PromptUpdate) -> PromptDoc | None:
        crud = PromptCRUD(db=db)
        doc_update = PromptDocUpdate(id=id, user_id=str(user.id), **prompt_update.model_dump(exclude_none=True))
        doc = await crud.update_one(doc_update=doc_update)

        match doc:
            case Exception():
                logger.bind(ctx=ctx, error=str(doc)).exception("Cannot update prompt")
                raise HTTPException(500, "Failed to update prompt")
            case PromptDoc():
                return doc
            case _:
                return None

    @staticmethod
    async def delete(ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, prompt_delete: PromptRead) -> int:
        crud = PromptCRUD(db=db)
        find_doc_read = PromptDocRead(user_id=str(user.id), **prompt_delete.model_dump(exclude_none=True))
        deleted = await crud.delete_many(doc_find=find_doc_read)

        match deleted:
            case DeleteResult():
                return deleted.deleted_count
            case Exception():
                logger.bind(ctx=ctx, error=str(deleted)).error("Cannot delete prompts")
                raise HTTPException(500, "Failed to delete prompts")