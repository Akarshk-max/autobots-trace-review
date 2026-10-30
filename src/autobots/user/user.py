import supabase_auth
from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.user.user_crud import UserCRUD
from src.autobots.user.user_orm_model import (
    UserCreate,
    UserDoc,
    UserDocCreate,
    UserDocRead,
    UserDocsPage,
    UserDocUpdate,
    UserORM,
    UserRead,
    UserUpdate,
)


class User:
    @staticmethod
    async def create(
        ctx: Context, user: UserORM, user_create: UserCreate, db: AsyncIOMotorDatabase
    ) -> UserDoc:
        user_dict = user.model_dump()
        user_create_dict = user_create.model_dump()
        user_dict.update(user_create_dict)
        user_doc_create = UserDocCreate(user_id=user.id, **user_dict)
        user_crud = UserCRUD(db=db)
        user_doc = await user_crud.insert_one(create_doc=user_doc_create)
        return UserDoc.model_validate(user_doc)

    @staticmethod
    async def get(ctx: Context, user: UserORM, id: str, db: AsyncIOMotorDatabase) -> UserDoc:
        # `find_one`, not `find_page`: the latter also runs a count, and this is
        # on the auth path for every authenticated request. Duplicate rows are a
        # separate concern (#200); the newest wins rather than failing the call.
        user_crud = UserCRUD(db=db)
        doc_find = UserDocRead(id=id, user_id=user.id)
        user_doc = await user_crud.find_one(doc_find=doc_find)
        if user_doc is None:
            raise HTTPException(status_code=404, detail="User not found")
        return UserDoc.model_validate(user_doc)

    _PROVIDER_OWNED = ("email", "user_metadata")

    @staticmethod
    async def _refresh_identity(
        ctx: Context,
        stored: UserDoc,
        token_user: supabase_auth.User,
        db: AsyncIOMotorDatabase,
    ) -> UserDoc:
        """Runs on every authenticated request, so it writes only on a change."""
        changed: dict = {}
        for field in User._PROVIDER_OWNED:
            incoming = getattr(token_user, field, None)
            if not incoming:
                continue
            current = getattr(stored, field, None)
            if isinstance(incoming, dict):
                # A token reports what its provider knows, not what is absent
                # everywhere: a password sign-in carries only `email_verified`.
                merged = {**(current or {}), **incoming}
                if merged != (current or {}):
                    changed[field] = merged
            elif incoming != current:
                changed[field] = incoming
        if not changed:
            return stored

        try:
            await UserCRUD(db=db).set_identity_fields(user_id=stored.user_id, fields=changed)
        except Exception as e:
            # Provisioning runs inside auth; a stale name must not fail the request.
            logger.bind(ctx=ctx, user_id=stored.user_id).warning(
                f"Could not refresh user identity: {e}"
            )
            return stored

        logger.bind(ctx=ctx, user_id=stored.user_id, fields=sorted(changed)).info(
            "Refreshed user identity from token"
        )
        return stored.model_copy(update=changed)

    @staticmethod
    async def create_if_not_exist(
        ctx: Context,
        supabase_user: supabase_auth.UserResponse,
        user_create: UserCreate,
        db: AsyncIOMotorDatabase = next(get_mongo_db()),
    ) -> UserDoc:
        try:
            user = UserORM(**supabase_user.user.model_dump())
            user_doc = await User.get(ctx=ctx, user=user, id=supabase_user.user.id, db=db)
            user_doc = UserDoc.model_validate(user_doc)
            user_doc = await User._refresh_identity(
                ctx=ctx, stored=user_doc, token_user=supabase_user.user, db=db
            )
            logger.bind(ctx=ctx, id=user_doc.id, user_id=user_doc.user_id).info("Existing user")
            return user_doc
        except HTTPException as e:
            if e.status_code != 404:
                raise
        try:
            user = UserORM(**supabase_user.user.model_dump())
            user_doc = await User.create(ctx=ctx, user=user, user_create=user_create, db=db)
            user_doc = UserDoc.model_validate(user_doc)
            logger.bind(ctx=ctx, id=user_doc.id, user_id=user_doc.user_id).info("Created new user")
            return user_doc
        except Exception:
            pass

    @staticmethod
    async def read(
        ctx: Context, db: AsyncIOMotorDatabase, user_id: str, user_read: UserRead
    ) -> UserDocsPage:
        user_crud = UserCRUD(db=db)
        doc_find = UserDocRead(user_id=user_id, **user_read.model_dump(exclude_none=True))
        doc_page = await user_crud.find_page(doc_find=doc_find)
        user_page = UserDocsPage.model_validate(doc_page.model_dump(exclude_none=True))
        return user_page

    @staticmethod
    async def update(
        ctx: Context, db: AsyncIOMotorDatabase, id: str, user_update: UserUpdate
    ) -> UserDoc:
        user_crud = UserCRUD(db=db)
        user_update.id = id
        doc_update = UserDocUpdate(**user_update.model_dump(exclude_none=True))
        user_doc = await user_crud.update_one(doc_update=doc_update)
        return UserDoc.model_validate(user_doc)

    @staticmethod
    async def delete(ctx: Context, user: UserORM, id: str, db: AsyncIOMotorDatabase) -> int:
        user_crud = UserCRUD(db=db)
        assert user.id == id, "Expected user id:{} to be same as id:{}".format(user.id, id)
        doc_find = UserDocRead(id=id, user_id=user.id)
        deleted = await user_crud.delete_many(doc_find=doc_find)
        return deleted.deleted_count
    
    @staticmethod
    async def get_non_existent_user_ids(
        ctx: Context, db: AsyncIOMotorDatabase, user_ids: list[str]
    ) -> list[str]:
        """Return the subset of user_ids that do not exist in the Users collection.

        Uses a single distinct query to fetch only existing user_id values,
        then computes the set difference in Python. Memory efficient — only
        the user_id strings are transferred, not full user documents.
        """
        if not user_ids:
            return []

        user_crud = UserCRUD(db=db)
        existing_ids: list[str] = await user_crud.document.distinct(
            "user_id", {"user_id": {"$in": user_ids}}
        )
        existing_set = set(existing_ids)
        return [uid for uid in user_ids if uid not in existing_set]