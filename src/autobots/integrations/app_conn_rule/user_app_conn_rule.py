from http import HTTPStatus
from typing import List

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.results import DeleteResult

from src.autobots import SettingsProvider
from src.autobots.access.access_doc_model import AccessFind
from src.autobots.access.user_access import UserAccess
from src.autobots.data_model.context import Context
from src.autobots.data_model.ownership import DEFAULT_OWNERSHIP, Ownership
from src.autobots.integrations.app_conn_rule.app_conn_rule_crud import ConnRuleCRUD
from src.autobots.integrations.app_conn_rule.app_conn_rule_model import (
    AppConnRuleCreate,
    AppConnRuleDoc,
    AppConnRuleDocCreate,
    AppConnRuleDocRead,
    AppConnRuleDocUpdate,
    AppConnRuleLiteDocPage,
    AppConnRuleRead,
    AppConnRuleUpdate,
)
from src.autobots.integrations.app_types import AppTypes
from src.autobots.user.user_orm_model import UserORM


class UserAppConnRule:
    @staticmethod
    async def create(
        ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, conn_rule_create: AppConnRuleCreate
    ) -> AppConnRuleDoc | None:
        crud = ConnRuleCRUD(db=db)
        conn_rule_doc = await UserAppConnRule.get_by_app_type(
            ctx=ctx, db=db, user=user, app_type=conn_rule_create.app_type
        )
        if conn_rule_doc:
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT, detail="There is already a Conn Rule for this app"
            )
        assert conn_rule_doc is None
        create_doc = AppConnRuleDocCreate(
            user_id=str(user.id), **conn_rule_create.model_dump(exclude_none=True)
        )
        doc = await crud.insert_one(create_doc)
        match doc:
            case Exception():
                logger.bind(ctx=ctx, error=str(doc)).exception("Cannot create conn rule")
            case _:
                conn_rule_doc = AppConnRuleDoc.model_validate(doc)
                logger.bind(ctx=ctx).info(f"Created conn rule: {conn_rule_doc.name}")
        return conn_rule_doc

    @staticmethod
    async def list(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        conn_rule_read: AppConnRuleRead,
        limit: int = 100,
        offset: int = 0,
    ) -> AppConnRuleLiteDocPage:
        crud = ConnRuleCRUD(db=db)
        find_doc_read = AppConnRuleDocRead(
            user_id=str(user.id), **conn_rule_read.model_dump(exclude_none=True)
        )
        # find_doc = ConnRuleDocReads(find_doc_read)
        page = await crud.find_page(doc_find=find_doc_read, limit=limit, offset=offset)
        page = AppConnRuleLiteDocPage.model_validate(page.model_dump(exclude_none=True))
        return page

    @staticmethod
    async def get(
        ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, id: str
    ) -> AppConnRuleDoc | None:
        crud = ConnRuleCRUD(db=db)
        doc_find_read = AppConnRuleDocRead(user_id=str(user.id), id=id)
        # doc_find_published_read = PublishedCharacterDocRead(id=character_id)
        doc = await crud.find_one(
            doc_find=doc_find_read
        )  # , or_find_queries=[doc_find_published_read])
        return AppConnRuleDoc.model_validate(doc) if doc else None

    @staticmethod
    async def get_by_app_type(
        ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, app_type: AppTypes
    ) -> AppConnRuleDoc | None:
        try:
            crud = ConnRuleCRUD(db=db)
            doc_find_read = AppConnRuleDocRead(user_id=str(user.id), app_type=app_type)
            doc = await crud.find_one(doc_find=doc_find_read)
            if doc:
                logger.bind(ctx=ctx, app_type=app_type, doc=doc).info(
                    f"Get conn rule for app type: {app_type}"
                )
                return AppConnRuleDoc.model_validate(doc)
        except Exception as e:
            logger.bind(ctx=ctx, error=str(e)).exception(
                f"Cannot get conn rule for app type: {app_type}"
            )
        return None

    @staticmethod
    async def get_ownership(
        ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, app_type: AppTypes
    ) -> List[Ownership]:
        settings = SettingsProvider.sget()
        ownership = list(DEFAULT_OWNERSHIP)
        try:
            conn_rule_doc: AppConnRuleDoc | None = await UserAppConnRule.get_by_app_type(
                ctx=ctx, db=db, user=user, app_type=app_type
            )
            if conn_rule_doc and conn_rule_doc.ownership:
                ownership = conn_rule_doc.ownership
        except Exception as e:
            logger.bind(ctx=ctx, error=str(e)).error(
                f"Cannot get conn rule ownership for app type: {app_type}"
            )

        if (
            settings.ALLOW_SYSTEM_CONN_TO_SYSTEM_DOMAIN
            and user.email
            and SettingsProvider.is_system_email(user.email)
        ):
            if Ownership.system not in ownership:
                ownership.append(Ownership.system)
            logger.bind(ctx=ctx, user=user, ownership=ownership).info("Added system ownership")

        # AUT-1459: Allow system OpenRouter fallback for Platform/Kiwi-Code subscribers
        if (
            app_type == AppTypes.openrouter
            and Ownership.system not in ownership
            and settings.OPENROUTER_API_KEY
        ):
            try:
                has_product_access = await UserAppConnRule._has_product_access(
                    ctx=ctx, db=db, user=user
                )
                if has_product_access:
                    ownership.append(Ownership.system)
                    logger.bind(ctx=ctx, app_type=app_type).info(
                        "Added system ownership for OpenRouter (product subscriber)"
                    )
            except Exception as e:
                logger.bind(ctx=ctx, error=str(e)).error(
                    "Failed to check product access for OpenRouter system fallback"
                )

        return ownership

    @staticmethod
    async def _has_product_access(
        ctx: Context, db: AsyncIOMotorDatabase, user: UserORM
    ) -> bool:
        """Check if user has a valid Platform or Kiwi-Code subscription."""
        user_access = UserAccess(user=user, db=db)
        access_docs = await user_access.list_user_access(
            ctx=ctx, access_find=AccessFind(status="valid"), limit=10, offset=0
        )
        for doc in access_docs.docs:
            if doc.subscription in ("Platform", "Kiwi-Code"):
                return True
        return False

    @staticmethod
    async def update(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        id: str,
        conn_rule_update: AppConnRuleUpdate,
    ) -> AppConnRuleDoc | None:
        crud = ConnRuleCRUD(db=db)
        doc_update = AppConnRuleDocUpdate(
            id=id, user_id=str(user.id), **conn_rule_update.model_dump(exclude_none=True)
        )
        doc = await crud.update_one(doc_update=doc_update)
        conn_rule_doc = None
        match doc:
            case Exception():
                logger.bind(ctx=ctx, error=str(doc)).exception("Cannot update conn rule")
            case AppConnRuleDoc():
                conn_rule_doc = doc
        return conn_rule_doc

    @staticmethod
    async def delete(
        ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, conn_rule_delete: AppConnRuleRead
    ) -> int | None:
        crud = ConnRuleCRUD(db=db)
        find_doc_read = AppConnRuleDocRead(
            user_id=str(user.id), **conn_rule_delete.model_dump(exclude_none=True)
        )
        deleted = await crud.delete_many(doc_find=find_doc_read)
        match deleted:
            case DeleteResult():
                return deleted.deleted_count
            case Exception():
                logger.bind(ctx=ctx, error=str(deleted)).error("Cannot delete conn rule")
        return None
