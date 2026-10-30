from typing import List

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.results import DeleteResult

from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context
from src.autobots.integrations.crypto.helpers import decrypt_if_needed, encrypt_if_needed
from src.autobots.integrations.db_auth.db_auth import DatabaseAuth
from src.autobots.integrations.db_auth.db_auth_crud import DbAuthCRUD
from src.autobots.integrations.db_auth.db_auth_model import (
    DB_AUTH_APP_NAME,
    DatabaseAuthModel,
    DbAuthCreate,
    DbAuthDoc,
    DbAuthDocCreate,
    DbAuthDocRead,
    DbAuthDocUpdate,
    DbAuthLiteDocPage,
    DbAuthRead,
    DbAuthUpdate,
)
from src.autobots.user.user_orm_model import UserORM


class UserDbAuth:
    """
    User-specific database authentication operations.
    Unlike regular app auth, database auth allows multiple connections per user.
    """

    @staticmethod
    async def create(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        db_auth_create: DbAuthCreate,
        decrypt: bool = False,
    ) -> DbAuthDoc | None:
        """
        Create database auth. Allows multiple auths per user (different database connections).
        """
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = DbAuthCRUD(db=db)
        DatabaseAuth.get_auth_model_type().model_validate(db_auth_create.auth)

        logger.bind(ctx=ctx, code=AppCode.DB_AUTH).info("Creating database auth (allowing multiple connections per user)")

        db_auth_create.auth = encrypt_if_needed(db_auth_create.auth)

        create_doc = DbAuthDocCreate(
            user_id=str(user.id), space_id=space_id, **db_auth_create.model_dump(exclude_none=True)
        )
        doc = await crud.insert_one(create_doc)
        logger.bind(ctx=ctx, code=AppCode.DB_AUTH).info(f"Created database auth for app_name: {db_auth_create.app_name}")
        match doc:
            case DbAuthDoc():
                if decrypt:
                    doc.auth = decrypt_if_needed(doc.auth)
                return DbAuthDoc.model_validate(doc)
            case _:
                logger.bind(ctx=ctx, code=AppCode.DB_AUTH).error(f"Cannot create database auth : {str(doc)}")
        return None

    @staticmethod
    async def list(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        db_auth_list: DbAuthRead | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> DbAuthLiteDocPage:
        """List all database auths for a user."""
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = DbAuthCRUD(db=db)
        find_doc = DbAuthDocRead(
            user_id=str(user.id),
            space_id=space_id,
            app_name=DB_AUTH_APP_NAME,
            **(db_auth_list.model_dump(exclude_none=True) if db_auth_list else {}),
        )
        page = await crud.find_page(doc_find=find_doc, limit=limit, offset=offset)
        db_auth_page = DbAuthLiteDocPage.model_validate(page.model_dump(exclude_none=True))
        return db_auth_page

    @staticmethod
    async def get(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        db_auth_id: str,
        decrypt: bool = False,
    ) -> DbAuthDoc | None:
        """Get a specific database auth by ID."""
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = DbAuthCRUD(db=db)
        doc_find = DbAuthDocRead(
            user_id=str(user.id), space_id=space_id, id=db_auth_id, app_name=DB_AUTH_APP_NAME
        )
        doc = await crud.find_one(doc_find=doc_find)
        if doc and decrypt:
            doc.auth = decrypt_if_needed(doc.auth)
        return DbAuthDoc.model_validate(doc) if doc else None

    @staticmethod
    async def get_by_name(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        connection_name: str,
        decrypt: bool = False,
    ) -> DbAuthDoc | None:
        """
        Get a database auth by its memorable name.
        Returns the first matching database auth with the given name for the user.
        """
        try:
            space_member = user.space_member
            if space_member and not space_member.space_id:
                raise HTTPException(400, "Invalid space member: missing space_id")
            space_id = space_member.space_id if space_member else None
            crud = DbAuthCRUD(db=db)
            find_dict = {
                "user_id": str(user.id),
                "app_name": DB_AUTH_APP_NAME,
                "name": connection_name,
            }
            if space_id:
                find_dict["space_id"] = space_id
            db_auth = await crud.document.find_one(find_dict)
            if not db_auth:
                logger.bind(ctx=ctx, code=AppCode.DB_AUTH, connection_name=connection_name).debug(
                    f"Cannot find database auth with name: {connection_name}"
                )
                return None
            db_auth["_id"] = str(db_auth.get("_id"))
            if decrypt:
                db_auth["auth"] = decrypt_if_needed(db_auth.get("auth") or {})
            db_auth_doc = DbAuthDoc.model_validate(db_auth)
            return db_auth_doc
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.DB_AUTH, connection_name=connection_name).exception(
                f"Error in get_by_name: {str(e)}"
            )
            return None

    @staticmethod
    async def update(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        id: str,
        db_auth_update: DbAuthUpdate,
        decrypt: bool = False,
    ) -> DbAuthDoc | None:
        """Update a database auth."""
        crud = DbAuthCRUD(db=db)
        DatabaseAuth.get_auth_model_type().model_validate(db_auth_update.auth)
        db_auth_update.auth = encrypt_if_needed(db_auth_update.auth)
        doc_update = DbAuthDocUpdate(
            id=id, user_id=str(user.id), **db_auth_update.model_dump(exclude_none=True)
        )
        doc = await crud.update_one(doc_update=doc_update)
        match doc:
            case DbAuthDoc():
                if decrypt:
                    doc.auth = decrypt_if_needed(doc.auth)
                return DbAuthDoc.model_validate(doc)
            case _:
                logger.bind(ctx=ctx, code=AppCode.DB_AUTH, error=str(doc)).error("Cannot update database auth")
        return None

    @staticmethod
    async def delete(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        db_auth_id: str,
    ) -> int | None:
        """Delete a specific database auth by ID."""
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = DbAuthCRUD(db=db)
        find_doc = DbAuthDocRead(
            user_id=str(user.id), space_id=space_id, id=db_auth_id, app_name=DB_AUTH_APP_NAME
        )
        deleted = await crud.delete_many(doc_find=find_doc)
        match deleted:
            case DeleteResult():
                return deleted.deleted_count
            case Exception():
                logger.bind(ctx=ctx, code=AppCode.DB_AUTH, error=str(deleted)).error("Cannot delete database auth")
        return None

    @staticmethod
    async def get_database_auth_model(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        db_auth_id: str,
    ) -> DatabaseAuthModel | None:
        """
        Get a DatabaseAuthModel by ID.
        Helper method for getting typed auth models.
        """
        db_auth_doc = await UserDbAuth.get(ctx=ctx, db=db, user=user, db_auth_id=db_auth_id, decrypt=True)
        if not db_auth_doc:
            return None
        return DatabaseAuth.get_auth_model_type().model_validate(db_auth_doc.auth)

    @staticmethod
    async def get_database_auth_model_by_name(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        connection_name: str,
    ) -> DatabaseAuthModel | None:
        """
        Get a DatabaseAuthModel by memorable name.
        Helper method for getting typed auth models.
        """
        db_auth_doc = await UserDbAuth.get_by_name(
            ctx=ctx, db=db, user=user, connection_name=connection_name, decrypt=True
        )
        if not db_auth_doc:
            return None
        return DatabaseAuth.get_auth_model_type().model_validate(db_auth_doc.auth)

    @staticmethod
    async def list_database_auth_models(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        limit: int = 100,
        offset: int = 0,
    ) -> List[DatabaseAuthModel]:
        """
        List all DatabaseAuthModels for a user.
        Helper method for getting typed auth models.
        """
        page = await UserDbAuth.list(ctx=ctx, db=db, user=user, limit=limit, offset=offset)

        database_auths = []
        for doc in page.docs:
            # Fetch full auth doc to get auth details
            full_doc = await UserDbAuth.get(ctx=ctx, db=db, user=user, db_auth_id=doc.id, decrypt=True)
            if full_doc:
                database_auth = DatabaseAuth.get_auth_model_type().model_validate(full_doc.auth)
                database_auths.append(database_auth)

        return database_auths
