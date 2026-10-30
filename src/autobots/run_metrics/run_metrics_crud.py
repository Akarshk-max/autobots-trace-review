from typing import Any, Dict

from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo import IndexModel

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.run_metrics.run_metrics_doc_model import (
    RunMetricsDoc,
    RunMetricsDocCreate,
    RunMetricsDocFind,
    RunMetricsDocUpdate,
)


class RunMetricsCRUD(
    CRUDBase[
        RunMetricsDoc,
        RunMetricsDoc,
        RunMetricsDocCreate,
        RunMetricsDocFind,
        RunMetricsDocUpdate,
        CollectionDocSearch,
    ]
):
    __indexes__ = [
        IndexModel(
            [
                ("user_id", 1),
                ("space_id", 1),
                ("is_root_run", 1),
                ("status", 1),
                ("created_at", -1),
            ],
            name="rm_user_space_time_v2",
            background=True,
        ),
        IndexModel(
            [
                ("space_id", 1),
                ("is_root_run", 1),
                ("status", 1),
                ("created_at", -1),
            ],
            name="rm_space_time_v2",
            background=True,
        ),
    ]

    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[RunMetricsDoc.__collection__]
        super().__init__(RunMetricsDoc, RunMetricsDoc, self.document)

    async def upsert_run(self, run_id: str, doc_create: RunMetricsDocCreate) -> None:
        """Keyed on the run's own id, so a retry or a resumed run rewrites rather than duplicates."""
        fields: Dict[str, Any] = doc_create.model_dump()
        await self.document.update_one({"_id": run_id}, {"$set": fields}, upsert=True)
