from typing import Any, Dict

from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase, DocFindPage
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.schedule.schedule_doc_model import (
    ScheduleDoc,
    ScheduleDocCreate,
    ScheduleDocFind,
    ScheduleDocUpdate,
    ScheduleFind,
)


class ScheduleCRUD(
    CRUDBase[
        ScheduleDoc,
        ScheduleDoc,
        ScheduleDocCreate,
        ScheduleDocFind,
        ScheduleDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        document: AsyncIOMotorCollection = db[ScheduleDoc.__collection__]
        super().__init__(ScheduleDoc, ScheduleDoc, document)


class ScheduleAdminCRUD(
    CRUDBase[
        ScheduleDoc,
        ScheduleDoc,
        ScheduleDocCreate,
        ScheduleFind,
        ScheduleDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        document: AsyncIOMotorCollection = db[ScheduleDoc.__collection__]
        super().__init__(ScheduleDoc, ScheduleDoc, document)

    async def read(self, find_params: Dict[str, Any], limit: int = 100, offset: int = 0) -> DocFindPage:
        docs = await self._find_raw_page(find_params=find_params, limit=limit, offset=offset)
        return docs
