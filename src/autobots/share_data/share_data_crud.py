from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.share_data.share_data_doc_model import (
    ShareDataDoc,
    ShareDataDocCreate,
    ShareDataDocRead,
    ShareDataDocUpdate,
    ShareDataRead,
)


class ShareDataCRUD(
    CRUDBase[
        ShareDataDoc,
        ShareDataDoc,
        ShareDataDocCreate,
        ShareDataDocRead,
        ShareDataDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[ShareDataDoc.__collection__]
        super().__init__(ShareDataDoc, ShareDataDoc, self.document)


class ShareDataAdminCRUD(
    CRUDBase[
        ShareDataDoc,
        ShareDataDoc,
        ShareDataDocCreate,
        ShareDataRead,
        ShareDataDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[ShareDataDoc.__collection__]
        super().__init__(ShareDataDoc, ShareDataDoc, self.document)
