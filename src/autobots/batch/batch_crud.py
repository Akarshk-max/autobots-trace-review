from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.batch.batch_doc_model import (
    BatchDoc,
    BatchDocCreate,
    BatchDocFind,
    BatchDocUpdate,
    BatchLiteDoc,
)
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch


class BatchCRUD(
    CRUDBase[
        BatchDoc,
        BatchLiteDoc,
        BatchDocCreate,
        BatchDocFind,
        BatchDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase):
        self.document: AsyncIOMotorCollection = db[BatchDoc.__collection__]
        super().__init__(BatchDoc, BatchLiteDoc, self.document)
