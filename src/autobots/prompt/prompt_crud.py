from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.prompt.prompt_doc_model import (
    PromptDoc,
    PromptDocCreate,
    PromptDocReads,
    PromptDocUpdate,
    PromptLiteDoc,
)


class PromptCRUD(
    CRUDBase[
        PromptDoc,
        PromptLiteDoc,
        PromptDocCreate,
        PromptDocReads,
        PromptDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[PromptDoc.__collection__]
        super().__init__(PromptDoc, PromptLiteDoc, self.document)