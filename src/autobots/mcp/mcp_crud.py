from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.mcp.mcp_doc_model import MCPDoc, MCPDocCreate, MCPDocFind, MCPDocUpdate


class MCPCRUD(
    CRUDBase[
        MCPDoc,
        MCPDoc,
        MCPDocCreate,
        MCPDocFind,
        MCPDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[MCPDoc.__collection__]
        super().__init__(MCPDoc, MCPDoc, self.document)
