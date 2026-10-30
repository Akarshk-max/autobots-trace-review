from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo import IndexModel

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.skills.skill_doc_model import (
    SkillDoc,
    SkillDocCreate,
    SkillDocRead,
    SkillDocUpdate,
    SkillLiteDoc,
)


class SkillCRUD(
    CRUDBase[
        SkillDoc,
        SkillLiteDoc,
        SkillDocCreate,
        SkillDocRead,
        SkillDocUpdate,
        CollectionDocSearch,
    ]
):
    # Serves the shared branch of the owned-or-shared filter, sort included.
    __indexes__ = [
        IndexModel(
            [("shared_with", 1), ("space_id", 1), ("updated_at", -1)],
            name="sk_shared_with_idx",
            background=True,
        )
    ]

    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[SkillDoc.__collection__]
        super().__init__(SkillDoc, SkillLiteDoc, self.document)
