from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.integrations.slack.slack_runnable_doc_model import (
    SlackRunnableDoc,
    SlackRunnableDocCreate,
    SlackRunnableDocRead,
    SlackRunnableDocUpdate,
    SlackRunnableLiteDoc,
)


class SlackRunnableCRUD(
    CRUDBase[
        SlackRunnableDoc,
        SlackRunnableLiteDoc,
        SlackRunnableDocCreate,
        SlackRunnableDocRead,
        SlackRunnableDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[SlackRunnableDoc.__collection__]
        super().__init__(SlackRunnableDoc, SlackRunnableLiteDoc, self.document)
