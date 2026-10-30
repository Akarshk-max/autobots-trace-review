from http import HTTPStatus
from typing import List

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.results import DeleteResult

from src.autobots import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.data_model.space_id import SpaceId
from src.autobots.skills.skill_crud import SkillCRUD
from src.autobots.skills.skill_doc_model import (
    SkillCreate,
    SkillDoc,
    SkillDocCreate,
    SkillDocRead,
    SkillDocReadSystem,
    SkillDocUpdate,
    SkillLiteDocPage,
    SkillRead,
    SkillUpdate,
)
from src.autobots.user.user_orm_model import UserORM


class UserSkills:
    @staticmethod
    async def create(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        skill_create: SkillCreate,
    ) -> SkillDoc:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = SkillCRUD(db=db)

        if len(skill_create.blocks.blocks) > 50:
            raise HTTPException(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Too many blocks"
            )  # 50 is enough

        existing = await UserSkills.get_by_name(ctx=ctx, db=db, user=user, name=skill_create.name)
        if existing:
            raise HTTPException(HTTPStatus.CONFLICT, "There is already a skill by this name")

        create_doc = SkillDocCreate(
            user_id=str(user.id),
            space_id=space_id,
            **skill_create.model_dump(exclude_none=True),
        )
        doc = await crud.insert_one(create_doc)

        match doc:
            case Exception():
                logger.bind(ctx=ctx, error=str(doc)).exception("Cannot create skill")
                raise HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, "Failed to create skill")
            case _:
                return SkillDoc.model_validate(doc)

    @staticmethod
    async def list(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        skill_read: SkillRead,
        limit: int = 100,
        offset: int = 0,
    ) -> SkillLiteDocPage:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = SkillCRUD(db=db)
        find_doc_read = SkillDocRead(
            user_id=str(user.id),
            space_id=space_id,
            **skill_read.model_dump(exclude_none=True),
        )
        page = await crud.find_page(doc_find=find_doc_read, limit=limit, offset=offset)
        skills_page = SkillLiteDocPage.model_validate(page.model_dump(exclude_none=True))
        return skills_page

    @staticmethod
    async def get(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        skill_id: str,
    ) -> SkillDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = SkillCRUD(db=db)
        doc_find_read = SkillDocRead(user_id=str(user.id), space_id=space_id, id=skill_id)
        doc = await crud.find_one(doc_find=doc_find_read)
        return SkillDoc.model_validate(doc) if doc else None

    @staticmethod
    async def get_by_name(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        name: str,
    ) -> SkillDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = SkillCRUD(db=db)
        doc_find_read = SkillDocRead(user_id=str(user.id), space_id=space_id, name=name)
        doc = await crud.find_one(doc_find=doc_find_read)
        return SkillDoc.model_validate(doc) if doc else None

    @staticmethod
    async def update(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        id: str,
        skill_update: SkillUpdate,
    ) -> SkillDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        crud = SkillCRUD(db=db)

        if skill_update.blocks and len(skill_update.blocks.blocks) > 50:
            raise HTTPException(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Too many blocks"
            )  # 50 is enough

        # enforce unique name (per user) on rename
        if skill_update.name:
            existing = await UserSkills.get_by_name(
                ctx=ctx, db=db, user=user, name=skill_update.name
            )
            if existing and str(existing.id) != str(id):
                raise HTTPException(HTTPStatus.CONFLICT, "There is already a skill by this name")

        doc_update = SkillDocUpdate(
            id=id, user_id=str(user.id), **skill_update.model_dump(exclude_none=True)
        )
        doc = await crud.update_one(doc_update=doc_update)

        match doc:
            case Exception():
                logger.bind(ctx=ctx, error=str(doc)).exception("Cannot update skill")
                raise HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, "Failed to update skill")
            case SkillDoc():
                return doc
            case _:
                return None

    @staticmethod
    async def delete(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        skill_delete: SkillRead,
    ) -> int:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = SkillCRUD(db=db)
        find_doc_read = SkillDocRead(
            user_id=str(user.id), space_id=space_id, **skill_delete.model_dump(exclude_none=True)
        )
        deleted = await crud.delete_many(doc_find=find_doc_read)

        match deleted:
            case DeleteResult():
                return deleted.deleted_count
            case Exception():
                logger.bind(ctx=ctx, error=str(deleted)).error("Cannot delete skills")
                raise HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, "Failed to delete skills")

    @staticmethod
    async def list_system_skills(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        skill_read: SkillRead,
        limit: int = 100,
        offset: int = 0,
    ) -> SkillLiteDocPage:
        system_user = SettingsProvider.sget().SYSTEM_USER
        crud = SkillCRUD(db=db)
        find_doc = SkillDocReadSystem(
            user_id=system_user,
            is_published=True,
            **skill_read.model_dump(exclude_none=True),
        )
        page = await crud.find_page(doc_find=find_doc, limit=limit, offset=offset)
        return SkillLiteDocPage.model_validate(page.model_dump(exclude_none=True))

    @staticmethod
    async def get_system_skill(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        skill_id: str,
    ) -> SkillDoc | None:
        system_user = SettingsProvider.sget().SYSTEM_USER
        crud = SkillCRUD(db=db)
        doc_find = SkillDocReadSystem(user_id=system_user, is_published=True, id=skill_id)
        doc = await crud.find_one(doc_find=doc_find)
        return SkillDoc.model_validate(doc) if doc else None

    @staticmethod
    async def get_shared_skill(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        skill_id: str,
    ) -> SkillDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        try:
            crud = SkillCRUD(db=db)
            # Check email-based sharing
            doc_find = SkillDocReadSystem(id=skill_id, shared_with=user.email, space_id=space_id)
            or_find_queries = None
            # Also check space-based sharing
            if space_id:
                or_find_queries = [
                    SkillDocReadSystem(id=skill_id, shared_with=SpaceId(space_id=space_id))
                ]
            doc = await crud.find_one(doc_find=doc_find, or_find_queries=or_find_queries)
            return SkillDoc.model_validate(doc) if doc else None
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
        return None

    @staticmethod
    async def get_owned_or_shared_or_system_skill(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        skill_id: str,
    ) -> SkillDoc | None:
        # Try owned first
        skill = await UserSkills.get(
            ctx=ctx,
            db=db,
            user=user,
            skill_id=skill_id,
        )
        if skill:
            return skill
        # Try shared (email-based + space-based)
        skill = await UserSkills.get_shared_skill(
            ctx=ctx,
            db=db,
            user=user,
            skill_id=skill_id,
        )
        if skill:
            return skill
        # Try system
        return await UserSkills.get_system_skill(ctx=ctx, db=db, skill_id=skill_id)
    
    @staticmethod
    async def get_system_skills_by_name(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        skill_name: str,
        limit: int = 100,
    ) -> List[SkillDoc]:
        system_user = SettingsProvider.sget().SYSTEM_USER
        crud = SkillCRUD(db=db)
        doc_find = SkillDocReadSystem(user_id=system_user, is_published=True, name=skill_name)
        page = await crud.get_by_name(doc_find=doc_find, limit=limit)
        return page.docs
        
        

    
