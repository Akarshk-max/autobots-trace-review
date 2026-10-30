from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.integrations.app_creds.app_creds_model import (
    AppCredsDoc,
    AppCredsDocCreate,
    AppCredsDocRead,
    AppCredsDocUpdate,
    AppCredsLiteDoc,
)


class AppCredsCRUD(
    CRUDBase[
        AppCredsDoc,
        AppCredsLiteDoc,
        AppCredsDocCreate,
        AppCredsDocRead,
        AppCredsDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        document: AsyncIOMotorCollection = db[AppCredsDoc.__collection__]
        super().__init__(AppCredsDoc, AppCredsLiteDoc, document)
