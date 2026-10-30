from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.characters.character_doc_model import (
    CharacterDoc,
    CharacterDocCreate,
    CharacterDocReads,
    CharacterDocUpdate,
    CharacterLiteDoc,
)
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch


class CharacterCRUD(
    CRUDBase[
        CharacterDoc,
        CharacterLiteDoc,
        CharacterDocCreate,
        CharacterDocReads,
        CharacterDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[CharacterDoc.__collection__]
        super().__init__(CharacterDoc, CharacterLiteDoc, self.document)
