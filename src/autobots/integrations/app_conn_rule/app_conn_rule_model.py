from typing import List

from pydantic import BaseModel, ConfigDict

from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.id import ID, OptionalID
from src.autobots.data_model.MongoId import MongoID
from src.autobots.data_model.ownership import DEFAULT_OWNERSHIP, Ownership
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId
from src.autobots.integrations.app_types import AppTypes


class AppConnRuleCreate(BaseModel):
    app_type: AppTypes
    ownership: List[Ownership] = list(DEFAULT_OWNERSHIP)


class AppConnRuleDocCreate(UserId, AppConnRuleCreate):
    pass


class AppConnRuleUpdate(BaseModel):
    app_type: AppTypes | None = None
    ownership: List[Ownership] | None = None


class AppConnRuleDocUpdate(UserId, ID, AppConnRuleUpdate):
    pass


class AppConnRuleRead(BaseModel):
    app_type: AppTypes | None = None
    ownership: List[Ownership] | None = None


class AppConnRuleDocRead(UserId, OptionalID, AppConnRuleRead):
    pass


class AppConnRuleDoc(MongoID, AppConnRuleDocCreate, CreatedAt, UpdatedAt):
    __collection__ = "AppConnRules"

    model_config = ConfigDict(populate_by_name=True)


class AppConnRuleLiteDocPage(DocFindPage):
    docs: List[AppConnRuleDoc]
