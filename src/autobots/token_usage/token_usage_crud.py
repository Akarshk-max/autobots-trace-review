from typing import Any, Dict, List, Tuple

from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo import IndexModel

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.token_usage.token_usage_doc_model import (
    TokenUsageDoc,
    TokenUsageDocCreate,
    TokenUsageDocFind,
    TokenUsageDocUpdate,
    TokenUsageLiteDoc,
)

TOKEN_FIELDS = [
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
]

COST_REPORTED_COUNT = "cost_reported_count"


def _group_stage(group_by: Any) -> Dict[str, Any]:
    return {
        "_id": group_by,
        "api_call_count": {"$sum": "$api_call_count"},
        "record_count": {"$sum": 1},
        "cost": {"$sum": "$cost"},
        COST_REPORTED_COUNT: {"$sum": {"$cond": [{"$gt": ["$cost", None]}, 1, 0]}},
        **{field: {"$sum": f"${field}"} for field in TOKEN_FIELDS},
    }


def _without_unreported_cost(group: Dict[str, Any]) -> Dict[str, Any]:
    """A provider that never reports a cost gets no cost, not a zero."""
    if not group.pop(COST_REPORTED_COUNT, 0):
        group["cost"] = None
    return group


class TokenUsageCRUD(
    CRUDBase[
        TokenUsageDoc,
        TokenUsageLiteDoc,
        TokenUsageDocCreate,
        TokenUsageDocFind,
        TokenUsageDocUpdate,
        CollectionDocSearch,
    ]
):
    __indexes__ = [
        IndexModel(
            [("user_id", 1), ("created_at", -1)],
            name="tu_user_created_idx",
            background=True,
        ),
        IndexModel(
            [("action_run_id", 1), ("created_at", -1)],
            name="tu_action_run_idx",
            background=True,
        ),
        IndexModel(
            [("action_graph_result_id", 1), ("created_at", -1)],
            name="tu_graph_result_idx",
            background=True,
        ),
        IndexModel(
            [("source", 1), ("source_id", 1), ("created_at", -1)],
            name="tu_source_idx",
            background=True,
        ),
        IndexModel(
            [("space_id", 1), ("created_at", -1)],
            name="tu_space_created_idx",
            background=True,
        ),
    ]

    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[TokenUsageDoc.__collection__]
        super().__init__(TokenUsageDoc, TokenUsageLiteDoc, self.document)

    async def sum_totals(self, match: Dict[str, Any]) -> Dict[str, Any]:
        pipeline: List[Dict[str, Any]] = [{"$match": match}, {"$group": _group_stage(None)}]
        async for group in self.document.aggregate(pipeline):
            group.pop("_id", None)
            return _without_unreported_cost(group)
        return {
            "api_call_count": 0,
            "record_count": 0,
            "cost": None,
            **{field: 0 for field in TOKEN_FIELDS},
        }

    async def sum_totals_by_provider(self, match: Dict[str, Any]) -> List[Dict[str, Any]]:
        pipeline: List[Dict[str, Any]] = [
            {"$match": match},
            {"$group": _group_stage("$provider")},
            {"$sort": {"total_tokens": -1}},
        ]
        groups: List[Dict[str, Any]] = []
        async for group in self.document.aggregate(pipeline):
            group["provider"] = group.pop("_id")
            groups.append(_without_unreported_cost(group))
        return groups

    async def find_all(self, match: Dict[str, Any], limit: int = 1000) -> List[TokenUsageLiteDoc]:
        docs: List[TokenUsageLiteDoc] = []
        cursor = self.document.find(match).sort("created_at", 1).limit(limit)
        async for doc in cursor:
            doc["_id"] = str(doc.get("_id"))
            docs.append(TokenUsageLiteDoc.model_validate(doc))
        return docs

    async def find_page(
        self, match: Dict[str, Any], limit: int = 100, offset: int = 0
    ) -> Tuple[List[TokenUsageLiteDoc], int]:
        total_count = await self.document.count_documents(match)
        docs: List[TokenUsageLiteDoc] = []
        cursor = self.document.find(match).sort("created_at", -1).skip(offset).limit(limit)
        async for doc in cursor:
            doc["_id"] = str(doc.get("_id"))
            docs.append(TokenUsageLiteDoc.model_validate(doc))
        return docs, total_count
