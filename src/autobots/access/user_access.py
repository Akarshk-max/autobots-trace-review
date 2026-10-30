from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from src.autobots.access.access_crud import AccessCRUD
from src.autobots.access.access_doc_model import (
    AccessCreate,
    AccessDoc,
    AccessDocCreate,
    AccessDocFind,
    AccessDocsFound,
    AccessDocUpdate,
    AccessFind,
    AccessStatus,
    AccessSubscription,
    AccessUpdate,
)
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)



class UserAccess:
    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
        self.user = user
        self.user_id = str(user.id)
        self.db = db
        self.access_crud = AccessCRUD(db)
        self.space_member = self.user.space_member

    # -----------------------------
    # Webhook / system helpers
    # -----------------------------
    async def upsert_user_access_by_subscription(
        self,
        ctx: Context,
        *,
        subscription: AccessSubscription,
        set_fields: dict[str, Any],
    ) -> None:
        """Upsert (create if missing) the user's access row for a subscription.

        This is the webhook-friendly counterpart of update_user_access_by_subscription
        (which 404s when missing).
        """
        try:
            if self.space_member and not self.space_member.space_id:
                raise HTTPException(400, "Invalid space member: missing space_id")
            space_id = self.space_member.space_id if self.space_member else None

            now = _utcnow()
            set_fields = {**(set_fields or {}), "updated_at": now}
            await self.access_crud.document.update_one(
                filter={
                    "user_id": self.user_id,
                    "space_id": space_id,
                    "subscription": subscription,
                },
                update={
                    "$setOnInsert": {
                        "user_id": self.user_id,
                        "space_id": space_id,
                        "subscription": subscription,
                        "created_at": now,
                    },
                    "$set": set_fields,
                },
                upsert=True,
            )
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACCESS).exception(str(e))
            raise HTTPException(status_code=500, detail=str(e))

    async def delete_user_access_by_subscription_and_status(
        self,
        ctx: Context,
        *,
        subscription: AccessSubscription,
        status: AccessStatus,
        space_id: str | None = None,
        derive_space_id: bool = True,
    ) -> int:
        """Delete the user's access rows for a subscription only if they match the given status."""
        try:
            if derive_space_id:
                if self.space_member and not self.space_member.space_id:
                    raise HTTPException(400, "Invalid space member: missing space_id")
                space_id = self.space_member.space_id if self.space_member else None

            res = await self.access_crud.document.delete_many(
                {
                    "user_id": self.user_id,
                    "space_id": space_id,
                    "subscription": subscription,
                    "status": status,
                }
            )
            return int(res.deleted_count)
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACCESS).exception(str(e))
            raise HTTPException(status_code=500, detail=str(e))

    async def create_user_access(self, ctx: Context, access_create: AccessCreate) -> AccessDoc:

        try:
            if self.space_member and not self.space_member.space_id:
                raise HTTPException(400, "Invalid space member: missing space_id")
            space_id = self.space_member.space_id if self.space_member else None
            # Enforce 1 row per (user_id, subscription)
            existing = await self.access_crud.find_one(
                AccessDocFind(user_id=self.user_id, subscription=access_create.subscription,space_id=space_id)
            )
            if existing:
                raise HTTPException(
                    status_code=400,
                    detail="User access for this subscription already exists",
                )

            doc_create = AccessDocCreate(user_id=self.user_id,space_id=space_id, **access_create.model_dump())
            doc = await self.access_crud.insert_one(doc_create)
            if isinstance(doc, Exception):
                if isinstance(doc, DuplicateKeyError):
                    raise HTTPException(
                        status_code=409,
                        detail="User access for this subscription already exists",
                    )
                raise HTTPException(status_code=500, detail=str(doc))
            return doc
        except HTTPException as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACCESS).exception(str(e))
            raise
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACCESS).exception(str(e))
            raise HTTPException(status_code=500, detail=str(e))

    async def list_user_access(
        self,
        ctx: Context,
        access_find: AccessFind,
        limit: int = 100,
        offset: int = 0,
    ) -> AccessDocsFound:
        try:
            if self.space_member and not self.space_member.space_id:
                raise HTTPException(400, "Invalid space member: missing space_id")
            space_id = self.space_member.space_id if self.space_member else None
            doc_find = AccessDocFind(
                user_id=self.user_id,space_id=space_id,
                **access_find.model_dump(exclude_none=True, exclude={"user_id"}),
            )
            paged_docs = await self.access_crud.find_page(
                doc_find=doc_find,
                or_find_queries=None,
                limit=limit,
                offset=offset,
            )
            return AccessDocsFound(
                docs=paged_docs.docs,
                total_count=paged_docs.total_count,
                limit=paged_docs.limit,
                offset=paged_docs.offset,
            )
        except HTTPException as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACCESS).exception(str(e))
            raise
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACCESS).exception(str(e))
            raise HTTPException(status_code=500, detail=str(e))

    async def get_user_access(self, ctx: Context, access_id: str) -> AccessDoc | None:
        try:
            if self.space_member and not self.space_member.space_id:
                raise HTTPException(400, "Invalid space member: missing space_id")
            space_id = self.space_member.space_id if self.space_member else None
            doc = await self.access_crud.find_one(AccessDocFind(id=access_id, user_id=self.user_id,space_id=space_id))
            return doc
        except HTTPException as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACCESS).exception(str(e))
            raise
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACCESS).exception(str(e))
            raise HTTPException(status_code=500, detail=str(e))

    async def get_user_access_by_subscription(
        self, ctx: Context, subscription: AccessSubscription
    ) -> AccessDoc | None:
        try:
            if self.space_member and not self.space_member.space_id:
                raise HTTPException(400, "Invalid space member: missing space_id")
            space_id = self.space_member.space_id if self.space_member else None
            doc = await self.access_crud.find_one(
                AccessDocFind(user_id=self.user_id, subscription=subscription,space_id=space_id)
            )
            return doc
        except HTTPException as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACCESS).exception(str(e))
            raise
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACCESS).exception(str(e))
            raise HTTPException(status_code=500, detail=str(e))

    async def update_user_access(self, ctx: Context, access_id: str, update: AccessUpdate) -> AccessDoc:
        try:
            
            existing = await self.get_user_access(ctx=ctx, access_id=access_id)
            if not existing:
                raise HTTPException(status_code=404, detail="User access not found")
            doc_update = AccessDocUpdate(id=access_id, user_id=self.user_id, **update.model_dump(exclude_none=True))
            updated = await self.access_crud.update_one(doc_update)
            if isinstance(updated, Exception):
                if isinstance(updated, DuplicateKeyError):
                    raise HTTPException(
                        status_code=409,
                        detail="User access for this subscription already exists",
                    )
                raise HTTPException(status_code=500, detail=str(updated))
            return updated
        except HTTPException as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACCESS).exception(str(e))
            raise
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACCESS).exception(str(e))
            raise HTTPException(status_code=500, detail=str(e))

    async def update_user_access_by_subscription(
        self,
        ctx: Context,
        subscription: AccessSubscription,
        update: AccessUpdate,
    ) -> AccessDoc:
        try:
            if self.space_member and not self.space_member.space_id:
                raise HTTPException(400, "Invalid space member: missing space_id")
            space_id = self.space_member.space_id if self.space_member else None
            update_dict = update.model_dump(exclude_none=True)
            # if update.subscription is not None and update.subscription != subscription:
            #     raise HTTPException(
            #         status_code=400,
            #         detail="Cannot change subscription using by-subscription update endpoint",
            #     )
            if not update_dict:
                raise HTTPException(status_code=400, detail="No fields to update")

            update_dict["updated_at"] = _utcnow()

            updated = await self.access_crud.document.find_one_and_update(
                filter={"user_id": self.user_id, "subscription": subscription, "space_id" : space_id},
                update={"$set": update_dict},
                return_document=ReturnDocument.AFTER,
            )
            if not updated:
                raise HTTPException(status_code=404, detail="User access not found")
            updated["_id"] = str(updated.get("_id"))
            return AccessDoc.model_validate(updated)
        except HTTPException as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACCESS).exception(str(e))
            raise
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACCESS).exception(str(e))
            raise HTTPException(status_code=500, detail=str(e))

    async def delete_user_access(self, ctx: Context, access_id: str) -> int:
        try:
            if self.space_member and not self.space_member.space_id:
                raise HTTPException(400, "Invalid space member: missing space_id")
            space_id = self.space_member.space_id if self.space_member else None
            delete_result = await self.access_crud.delete_many(
                AccessDocFind(id=access_id, user_id=self.user_id, space_id=space_id)
            )
            return delete_result.deleted_count
        except HTTPException as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACCESS).exception(str(e))
            raise
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACCESS).exception(str(e))
            raise HTTPException(status_code=500, detail=str(e))

    async def delete_user_access_by_subscription(
        self,
        ctx: Context,
        subscription: AccessSubscription,
        *,
        space_id: str | None = None,
        derive_space_id: bool = True,
    ) -> int:
        try:
            if derive_space_id:
                if self.space_member and not self.space_member.space_id:
                    raise HTTPException(400, "Invalid space member: missing space_id")
                space_id = self.space_member.space_id if self.space_member else None
            delete_result = await self.access_crud.delete_many(
                AccessDocFind(user_id=self.user_id, subscription=subscription, space_id=space_id)
            )
            return delete_result.deleted_count
        except HTTPException as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACCESS).exception(str(e))
            raise
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACCESS).exception(str(e))
            raise HTTPException(status_code=500, detail=str(e))
