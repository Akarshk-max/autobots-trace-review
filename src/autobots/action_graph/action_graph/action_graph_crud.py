from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo import IndexModel

from src.autobots.action_graph.action_graph.action_graph_doc_model import (
    ActionGraphDoc,
    ActionGraphDocCreate,
    ActionGraphDocFind,
    ActionGraphDocUpdate,
    ActionGraphFind,
    ActionGraphLiteDoc,
)
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch


class ActionGraphCRUD(
    CRUDBase[
        ActionGraphDoc,
        ActionGraphLiteDoc,
        ActionGraphDocCreate,
        ActionGraphDocFind,
        ActionGraphDocUpdate,
        CollectionDocSearch,
    ]
):
    # Serves the shared branch of the owned-or-shared filter, sort included.
    __indexes__ = [
        IndexModel(
            [("shared_with", 1), ("space_id", 1), ("updated_at", -1)],
            name="ag_shared_with_idx",
            background=True,
        )
    ]

    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[ActionGraphDoc.__collection__]
        super().__init__(ActionGraphDoc, ActionGraphLiteDoc, self.document)


class ActionGraphCRUDForAdmin(
    CRUDBase[
        ActionGraphDoc,
        ActionGraphLiteDoc,
        ActionGraphDocCreate,
        ActionGraphFind,
        ActionGraphDocUpdate,
        CollectionDocSearch,
    ]
):
    """CRUD for admin/share flow - finds by id without user_id restriction."""

    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[ActionGraphDoc.__collection__]
        super().__init__(ActionGraphDoc, ActionGraphLiteDoc, self.document)
