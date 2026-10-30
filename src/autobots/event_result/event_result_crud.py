from datetime import datetime, timedelta
from typing import Any, Dict, List

from bson import ObjectId
from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo import DESCENDING, IndexModel

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase, DocFindPage
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_custom_search import (
    build_event_results_custom_search_filter,
)
from src.autobots.event_result.event_result_model import (
    EventResultDoc,
    EventResultDocCreate,
    EventResultDocFind,
    EventResultDocSearch,
    EventResultDocUpdate,
    EventResultLiteDoc,
    EventResultStatus,
    EventType,
)


class EventResultCRUD(
    CRUDBase[
        EventResultDoc,
        EventResultLiteDoc,
        EventResultDocCreate,
        EventResultDocFind,
        EventResultDocUpdate,
        EventResultDocSearch,
    ]
):
    # Serves the shared branch of the owned-or-shared filter, sort included.
    # `shared_with` leads because it is the selective term: an index leading with
    # `type` is what Mongo fell back to, scanning every run of that kind across
    # every user.
    __indexes__ = [
        IndexModel(
            [("shared_with", 1), ("type", 1), ("space_id", 1), ("updated_at", -1)],
            name="er_shared_with_idx",
            background=True,
        )
    ]

    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        document: AsyncIOMotorCollection = db[EventResultDoc.__collection__]
        super().__init__(EventResultDoc, EventResultLiteDoc, document)

    async def find_by_ids(
        self, ids: List[str], limit: int = 1000
    ) -> List[EventResultLiteDoc]:
        """Runs by id, unscoped - the caller decides which of them it may read.

        `_id` is an ObjectId here, so string ids have to be coerced or the query
        silently matches nothing.
        """
        if not ids:
            return []
        mongo_ids: List[Any] = []
        for id_value in ids:
            try:
                mongo_ids.append(ObjectId(str(id_value)))
            except Exception:
                mongo_ids.append(str(id_value))
        return await self._find_raw(find_params={"_id": {"$in": mongo_ids}}, limit=limit)

    async def count_by_status_for_batches(
        self,
        *,
        batch_ids: List[str],
        user_id: str | None = None,
        space_id: str | None = None,
    ) -> Dict[str, Dict[str, int]]:
        """Status counts per batch, as {batch_id: {status: count}}.

        Takes a list so a page of batches costs one round trip rather than one
        query each. Batch progress is derived from this on every read and never
        stored - the run paths retry and re-open results, so a stored counter
        would drift.
        """
        if not batch_ids:
            return {}

        match: Dict[str, Any] = {"batch_id": {"$in": batch_ids}}
        if user_id is not None:
            match["user_id"] = user_id
        if space_id is not None:
            match["space_id"] = space_id

        pipeline: List[Dict[str, Any]] = [
            {"$match": match},
            {
                "$group": {
                    "_id": {"batch_id": "$batch_id", "status": "$status"},
                    "count": {"$sum": 1},
                }
            },
        ]
        rows = await self.document.aggregate(pipeline).to_list(length=None)

        counts: Dict[str, Dict[str, int]] = {}
        for row in rows:
            key = row.get("_id") or {}
            batch_id = key.get("batch_id")
            status = key.get("status")
            if not batch_id or not status:
                continue
            counts.setdefault(batch_id, {})[status] = row.get("count", 0)
        return counts

    async def search_custom_page(
        self,
        *,
        user_id: str,
        event_type: EventType,
        q: str,
        limit: int = 100,
        offset: int = 0,
        searchable_paths: List[str] | None = None,
    ) -> DocFindPage:
        find_params: Dict[str, Any] = build_event_results_custom_search_filter(
            user_id=user_id,
            event_type=event_type,
            q=q,
            searchable_paths=searchable_paths,
        )

        docs = await self._find_raw_custom(
            find_params=find_params, limit=limit, offset=offset
        )
        total_count = await self.document.count_documents(find_params)
        return DocFindPage(
            docs=docs, total_count=total_count, limit=limit, offset=offset
        )

    async def _find_raw_custom(
        self, *, find_params: Dict[str, Any], limit: int = 100, offset: int = 0
    ) -> List[EventResultLiteDoc]:
        model_fields = self.lite_doc_model.model_fields.keys()
        fields_to_select: Dict[str, int] = {field: 1 for field in model_fields}

        cursor = self.document.find(find_params, fields_to_select)
        cursor.allow_disk_use(True)
        cursor.sort([("updated_at", DESCENDING)]).skip(offset * limit).limit(limit)

        docs: List[EventResultLiteDoc] = []
        async for doc in cursor:
            try:
                doc["_id"] = str(doc.get("_id"))
                docs.append(self.lite_doc_model.model_validate(doc))
            except Exception:
                # Keep search robust: skip malformed docs rather than failing the whole request.
                continue
        return docs

    async def find_with_date_range(
        self, doc_find: EventResultDocFind, limit: int = 100, offset: int = 0
    ) -> List[EventResultLiteDoc]:
        """Override find method to handle date range filtering"""
        query = await self._build_date_range_query(doc_find)

        cursor = self.document.find(query).skip(offset).limit(limit)
        docs = []
        async for doc in cursor:
            doc["_id"] = str(doc.get("_id"))
            event_result_doc = self.lite_doc_model.model_validate(doc)
            docs.append(event_result_doc)
        return docs

    async def find_page_with_date_range(
        self, doc_find: EventResultDocFind, limit: int = 100, offset: int = 0
    ) -> DocFindPage:
        """Override find_page method to handle date range filtering"""
        query = await self._build_date_range_query(doc_find)

        # Get documents
        cursor = self.document.find(query).skip(offset).limit(limit)
        docs = []
        async for doc in cursor:
            doc["_id"] = str(doc.get("_id"))
            event_result_doc = self.lite_doc_model.model_validate(doc)
            docs.append(event_result_doc)

        # Get total count
        total_count = await self.document.count_documents(query)

        return DocFindPage(docs=docs, total_count=total_count, limit=limit, offset=offset)

    async def _build_date_range_query(self, doc_find: EventResultDocFind) -> dict:
        """Build MongoDB query with date range support"""
        query = {}

        # Handle regular fields
        find_dict = doc_find.model_dump(exclude_none=True)
        for key, value in find_dict.items():
            if key not in ["created_at_gte", "created_at_lte"]:
                if key == "id":
                    query["_id"] = value
                elif hasattr(value, "value"):  # Handle enum values
                    query[key] = value.value
                else:
                    query[key] = value

        # Handle date range filters
        date_filters = {}
        if doc_find.created_at_gte:
            date_filters["$gte"] = doc_find.created_at_gte
        if doc_find.created_at_lte:
            date_filters["$lte"] = doc_find.created_at_lte

        if date_filters:
            query["created_at"] = date_filters

        return query


class EventResultAdminCRUD(
    CRUDBase[
        EventResultDoc,
        EventResultDoc,
        EventResultDocCreate,
        EventResultDocFind,
        EventResultDocUpdate,
        EventResultDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        document: AsyncIOMotorCollection = db[EventResultDoc.__collection__]
        super().__init__(EventResultDoc, EventResultDoc, document)

    async def get_stuck_graphs(
        self, ctx: Context, minutes: int = 120, limit: int = 3, offset: int = 0
    ) -> List[EventResultDoc]:
        # Calculate timestamp minutes ago
        threshold_time = datetime.now() - timedelta(minutes=minutes)

        # Query
        find_params = {
            "type": EventType.action_graph_run.value,
            "status": EventResultStatus.processing.value,
            "updated_at": {"$lt": threshold_time},
        }

        docs = await self._find_raw(find_params=find_params, limit=limit, offset=offset)
        return docs

    async def get_event_result_by_id(
        self, ctx: Context, event_doc_id: str
    ) -> EventResultDoc | None:
        find_params = {"_id": ObjectId(event_doc_id)}
        doc = await self._find_raw(find_params=find_params)
        return doc[0] if doc else None
