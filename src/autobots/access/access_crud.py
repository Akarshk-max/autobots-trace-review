from __future__ import annotations

from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.access.access_doc_model import (
    AccessDoc,
    AccessDocCreate,
    AccessDocFind,
    AccessDocUpdate,
    AccessLiteDoc,
)
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch


class AccessCRUD(
    CRUDBase[
        AccessDoc,
        AccessLiteDoc,
        AccessDocCreate,
        AccessDocFind,
        AccessDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[AccessDoc.__collection__]
        super().__init__(AccessDoc, AccessLiteDoc, self.document)

    async def find_one_by_stripe_subscription_id_unscoped(
        self, *, stripe_subscription_id: str, space_id: str | None
    ) -> AccessDoc | None:
        """Find an Access row by Stripe subscription id, optionally scoped by space_id.

        We cannot use CRUDBase.find_one here because it asserts user_id/space_id/id,
        and it may inject space_id=None depending on the Find model.
        """
        if not stripe_subscription_id:
            return None
        doc = await self.document.find_one(
            {
                "stripe_subscription_id": str(stripe_subscription_id),
                "space_id": str(space_id) if space_id is not None else None,
            },
            sort=[("updated_at", -1)],
        )
        if doc is None:
            return None
        doc["_id"] = str(doc.get("_id"))
        return AccessDoc.model_validate(doc)

    async def find_one_by_user_and_stripe_ids_unscoped(
        self,
        *,
        user_id: str,
        stripe_subscription_id: str,
        stripe_user_id: str | None,
        space_id: str | None,
    ) -> AccessDoc | None:
        """Find an Access row owned by user_id that matches the Stripe subscription and customer ids.

        Used by webhooks that must only update rows we already own, never create new ones.
        """
        if not user_id or not stripe_subscription_id:
            return None
        query: dict = {
            "user_id": str(user_id),
            "stripe_subscription_id": str(stripe_subscription_id),
            "space_id": str(space_id) if space_id is not None else None,
        }
        if stripe_user_id:
            query["stripe_user_id"] = str(stripe_user_id)
        doc = await self.document.find_one(query, sort=[("updated_at", -1)])
        if doc is None:
            return None
        doc["_id"] = str(doc.get("_id"))
        return AccessDoc.model_validate(doc)

    async def find_many_by_stripe_subscription_id_unscoped(
        self, *, stripe_subscription_id: str, space_id: str | None, limit: int = 1000
    ) -> list[AccessDoc]:
        """Find all Access rows by Stripe subscription id scoped by space_id."""
        if not stripe_subscription_id:
            return []
        cursor = self.document.find(
            {
                "stripe_subscription_id": str(stripe_subscription_id),
                "space_id": str(space_id) if space_id is not None else None,
            }
        ).sort([("updated_at", -1)]).limit(int(limit))
        docs: list[AccessDoc] = []
        async for d in cursor:
            d["_id"] = str(d.get("_id"))
            docs.append(AccessDoc.model_validate(d))
        return docs
