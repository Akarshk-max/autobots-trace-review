from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.integrations.app_creds.system_keys_model import (
    SystemCredsDoc,
    SystemCredsDocCreate,
    SystemCredsDocRead,
    SystemCredsDocUpdate,
    SystemCredsLiteDoc,
)


class SystemKeysCRUD(
    CRUDBase[
        SystemCredsDoc,
        SystemCredsLiteDoc,
        SystemCredsDocCreate,
        SystemCredsDocRead,
        SystemCredsDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        document: AsyncIOMotorCollection = db[SystemCredsDoc.__collection__]
        super().__init__(SystemCredsDoc, SystemCredsLiteDoc, document)
