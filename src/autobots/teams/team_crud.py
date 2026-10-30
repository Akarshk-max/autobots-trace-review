from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.teams.team_doc_model import (
    TeamDoc,
    TeamDocCreate,
    TeamDocRead,
    TeamDocUpdate,
    TeamLiteDoc,
)


class TeamCRUD(
    CRUDBase[TeamDoc, TeamLiteDoc, TeamDocCreate, TeamDocRead, TeamDocUpdate, CollectionDocSearch]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[TeamDoc.__collection__]
        super().__init__(TeamDoc, TeamLiteDoc, self.document)
