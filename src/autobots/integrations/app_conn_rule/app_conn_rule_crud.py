from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.integrations.app_conn_rule.app_conn_rule_model import (
    AppConnRuleDoc,
    AppConnRuleDocCreate,
    AppConnRuleDocRead,
    AppConnRuleDocUpdate,
)


class ConnRuleCRUD(
    CRUDBase[
        AppConnRuleDoc,
        AppConnRuleDoc,
        AppConnRuleDocCreate,
        AppConnRuleDocRead,
        AppConnRuleDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[AppConnRuleDoc.__collection__]
        super().__init__(AppConnRuleDoc, AppConnRuleDoc, self.document)
