from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.action.action.action_doc_model import (
    ActionDoc,
    ActionDocCreate,
    ActionDocFind,
    ActionDocUpdate,
    ActionFind,
    ActionLiteDoc,
)
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch


class ActionCRUD(
    CRUDBase[
        ActionDoc,
        ActionLiteDoc,
        ActionDocCreate,
        ActionDocFind,
        ActionDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[ActionDoc.__collection__]
        super().__init__(ActionDoc, ActionLiteDoc, self.document)


class ActionCRUDForAdmin(
    CRUDBase[
        ActionDoc,
        ActionLiteDoc,
        ActionDocCreate,
        ActionFind,
        ActionDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[ActionDoc.__collection__]
        super().__init__(ActionDoc, ActionLiteDoc, self.document)
