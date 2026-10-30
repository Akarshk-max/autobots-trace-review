# src/autobots/daytona/daytona_job_crud.py
from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.conn.daytona.state.daytona_job_model import DaytonaJobDoc, DaytonaJobDocCreate
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch


class DaytonaJobCRUD(
    CRUDBase[
        DaytonaJobDoc,
        DaytonaJobDoc,
        DaytonaJobDocCreate,
        DaytonaJobDoc,
        DaytonaJobDoc,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        document: AsyncIOMotorCollection = db[DaytonaJobDoc.__collection__]
        super().__init__(DaytonaJobDoc, DaytonaJobDoc, document)
