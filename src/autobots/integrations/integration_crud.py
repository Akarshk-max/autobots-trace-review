from datetime import UTC, datetime
from typing import Any, Dict

from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.integrations.integration_model import (
    IntegrationDoc,
    IntegrationDocCreate,
    IntegrationDocRead,
    IntegrationDocUpdate,
    IntegrationLiteDoc,
)


class IntegrationCRUD(
    CRUDBase[
        IntegrationDoc,
        IntegrationLiteDoc,
        IntegrationDocCreate,
        IntegrationDocRead,
        IntegrationDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[IntegrationDoc.__collection__]
        super().__init__(IntegrationDoc, IntegrationLiteDoc, self.document)

    async def set_creds(self, *, identity: Dict[str, Any], creds: Dict[str, Any]) -> None:
        """Put credentials on the record for this connection, creating it if it has none.

        Keyed on the four fields that name a connection rather than on an id, because the
        caller writing credentials knows which connection they belong to but not whether a
        record exists yet. A connection with only credentials is still a connection, so the
        record it gets carries an empty `auth` — not a missing one.

        The base class works in documents and ids; this is an upsert on identity, which it
        has no shape for, so it lives here rather than as raw collection access in a
        service.
        """
        now = datetime.now(UTC)
        result = await self.document.update_one(
            identity, {"$set": {"creds": creds, "updated_at": now}}
        )
        if result.matched_count:
            return
        await self.document.insert_one(
            {
                **identity,
                "creds": creds,
                "auth": {},
                "lookup": {},
                "default": True,
                "created_at": now,
                "updated_at": now,
            }
        )

    async def clear_creds(self, *, identity: Dict[str, Any]) -> None:
        """Take the credentials off this connection's record, and the record with them if
        that leaves nothing behind.

        A record still holding auth goes on working with its credentials removed. One left
        with neither half is not a connection any more, and an empty shell would still be
        listed as one.
        """
        doc = await self.document.find_one(identity)
        if not doc:
            return
        if doc.get("auth"):
            await self.document.update_one(
                {"_id": doc["_id"]},
                {"$set": {"creds": {}, "updated_at": datetime.now(UTC)}},
            )
        else:
            await self.document.delete_one({"_id": doc["_id"]})
