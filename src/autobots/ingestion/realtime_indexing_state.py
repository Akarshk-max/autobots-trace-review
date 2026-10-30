# src/autobots/ingestion/realtime_indexing_state.py
from datetime import datetime
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from pymongo import ReturnDocument


class RealtimeIndexingStateDoc(BaseModel):
    id: str = Field(..., alias="_id")  # collection_name
    # Note: All datetimes are stored as timezone-naive but represent UTC.
    # This is required to avoid BSON encoding errors with pymongo.
    last_processed_cursor_ts: datetime
    last_processed_cursor_id: str
    updated_at: datetime = Field(default_factory=lambda: datetime.utcnow())


class RealtimeIndexingStateCRUD:
    def __init__(self, db: AsyncIOMotorDatabase):
        self.collection: AsyncIOMotorCollection = db["RealtimeIndexingState"]

    async def get_state(self, collection_name: str) -> Optional[RealtimeIndexingStateDoc]:
        doc = await self.collection.find_one({"_id": collection_name})
        return RealtimeIndexingStateDoc.model_validate(doc) if doc else None

    async def update_cursor(
        self, collection_name: str, new_ts: datetime, new_id: str
    ) -> RealtimeIndexingStateDoc:
        # [V9 FIX] Ensure datetime is naive UTC before writing to DB to prevent BSON error.
        if new_ts.tzinfo is not None:
            new_ts = new_ts.replace(tzinfo=None)

        updated_doc = await self.collection.find_one_and_update(
            {"_id": collection_name},
            {
                "$set": {
                    "last_processed_cursor_ts": new_ts,
                    "last_processed_cursor_id": new_id,
                    "updated_at": datetime.utcnow(),
                }
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        # Guard to handle cases where the update operation fails and returns None.
        if not updated_doc:
            raise RuntimeError(
                f"Failed to update or create indexing state for '{collection_name}'."
            )
        return RealtimeIndexingStateDoc.model_validate(updated_doc)
