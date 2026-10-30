from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.webhook.webhook_model import (
    WebhookDoc,
    WebhookDocCreate,
    WebhookDocFind,
    WebhookDocUpdate,
)


class WebhookCRUD(
    CRUDBase[
        WebhookDoc,
        WebhookDoc,
        WebhookDocCreate,
        WebhookDocFind,
        WebhookDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[WebhookDoc.__collection__]
        super().__init__(WebhookDoc, WebhookDoc, self.document)
