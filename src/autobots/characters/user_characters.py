from http import HTTPStatus

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.results import DeleteResult

from src.autobots.characters.character_crud import CharacterCRUD
from src.autobots.characters.character_doc_model import (
    CharacterCreate,
    CharacterDoc,
    CharacterDocCreate,
    CharacterDocRead,
    CharacterDocUpdate,
    CharacterLiteDocPage,
    CharacterRead,
    CharacterUpdate,
    PublishedCharacterDocRead,
)
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


class UserCharacter:
    @staticmethod
    async def create(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        character_create: CharacterCreate,
    ) -> CharacterDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = CharacterCRUD(db=db)
        character_doc = None
        character_doc = await UserCharacter.get_by_name(
            ctx=ctx, db=db, user=user, name=character_create.name
        )
        if character_doc:
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT, detail="There is already a character by this name"
            )
        assert character_doc is None
        character_create.is_published = True
        create_doc = CharacterDocCreate(
            user_id=str(user.id),
            space_id=space_id,
            **character_create.model_dump(exclude_none=True),
        )
        doc = await crud.insert_one(create_doc)
        match doc:
            case Exception():
                logger.bind(ctx=ctx, error=str(doc)).exception("Cannot create character")
            case _:
                character_doc = CharacterDoc.model_validate(doc)
                logger.bind(ctx=ctx).info(f"Created character: {character_create.name}")
        return character_doc

    @staticmethod
    async def list(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        character_read: CharacterRead,
        limit: int = 100,
        offset: int = 0,
    ) -> CharacterLiteDocPage:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = CharacterCRUD(db=db)
        find_doc_read = CharacterDocRead(
            user_id=str(user.id),
            space_id=space_id,
            **character_read.model_dump(exclude_none=True),
        )
        # find_doc = CharacterDocReads(find_doc_read)
        page = await crud.find_page(doc_find=find_doc_read, limit=limit, offset=offset)
        character_page = CharacterLiteDocPage.model_validate(page.model_dump(exclude_none=True))
        return character_page

    @staticmethod
    async def get(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        character_id: str,
    ) -> CharacterDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = CharacterCRUD(db=db)
        doc_find_read = CharacterDocRead(user_id=str(user.id), space_id=space_id, id=character_id)
        # doc_find = CharacterDocReads(doc_find_read)
        doc_find_published_read = PublishedCharacterDocRead(id=character_id)
        # doc_find_published = CharacterDocReads(doc_find_published_read)
        doc = await crud.find_one(doc_find=doc_find_read, or_find_queries=[doc_find_published_read])
        return CharacterDoc.model_validate(doc) if doc else None

    @staticmethod
    async def get_by_name(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        name: str,
    ) -> CharacterDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = CharacterCRUD(db=db)
        doc_find_read = CharacterDocRead(user_id=str(user.id), space_id=space_id, name=name)
        # doc_find = CharacterDocReads(doc_find_read)
        doc = await crud.find_one(doc_find=doc_find_read)
        return CharacterDoc.model_validate(doc) if doc else None

    @staticmethod
    async def update(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        id: str,
        character_update: CharacterUpdate,
    ) -> CharacterDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        crud = CharacterCRUD(db=db)
        doc_update = CharacterDocUpdate(
            id=id, user_id=str(user.id), **character_update.model_dump(exclude_none=True)
        )
        doc = await crud.update_one(doc_update=doc_update)
        character_doc = None
        match doc:
            case Exception():
                logger.bind(ctx=ctx, error=str(doc)).exception("Cannot update character")
            case CharacterDoc():
                character_doc = doc
        return character_doc

    @staticmethod
    async def delete(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        character_delete: CharacterRead,
    ) -> int | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = CharacterCRUD(db=db)
        find_doc_read = CharacterDocRead(
            user_id=str(user.id),
            space_id=space_id,
            **character_delete.model_dump(exclude_none=True),
        )
        # find_doc = CharacterDocReads(find_doc_read)
        deleted = await crud.delete_many(doc_find=find_doc_read)
        match deleted:
            case DeleteResult():
                return deleted.deleted_count
            case Exception():
                logger.bind(ctx=ctx, error=str(deleted)).error("Cannot delete characters")
        return None
