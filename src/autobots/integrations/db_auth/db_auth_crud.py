from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.integrations.db_auth.db_auth_model import (
    DbAuthDoc,
    DbAuthDocCreate,
    DbAuthDocRead,
    DbAuthDocUpdate,
    DbAuthLiteDoc,
)


class DbAuthCRUD(
    CRUDBase[
        DbAuthDoc,
        DbAuthLiteDoc,
        DbAuthDocCreate,
        DbAuthDocRead,
        DbAuthDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[DbAuthDoc.__collection__]
        super().__init__(DbAuthDoc, DbAuthLiteDoc, self.document)
