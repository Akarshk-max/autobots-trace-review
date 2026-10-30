from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.session.session_doc_model import (
    SessionDoc,
    SessionDocCreate,
    SessionDocFind,
    SessionDocUpdate,
)


class SessionCRUD(
    CRUDBase[
        SessionDoc,
        SessionDoc,
        SessionDocCreate,
        SessionDocFind,
        SessionDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[SessionDoc.__collection__]
        super().__init__(SessionDoc, SessionDoc, self.document)
