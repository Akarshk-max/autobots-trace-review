from typing import List

from pydantic import BaseModel, ConfigDict, Field

from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId
from src.autobots.integrations.app_types import AppTypes


class IDN(BaseModel):
    id: str = Field(..., alias="_id")


class SystemCredsCreate(BaseModel):
    app_name: str | AppTypes
    name: str


class SystemCredsDocCreate(UserId, SystemCredsCreate, CreatedAt, UpdatedAt):
    pass


class SystemCredsRead(IDN):
    id: str | None = None
    app_name: AppTypes | None = None
    name: str | None = None


class SystemCredsDocRead(UserId, SystemCredsRead):
    pass


class SystemCredsUpdate(SystemCredsCreate):
    pass


class SystemCredsDocUpdate(IDN, UserId, SystemCredsUpdate):
    id: str


class SystemCredsDeleteDoc(SystemCredsDocRead):
    pass


class SystemCredsLiteDoc(IDN, UserId, CreatedAt, UpdatedAt):
    app_name: AppTypes
    name: str | None = None
    model_config = ConfigDict(populate_by_name=True)


class SystemCredsLiteDocPage(DocFindPage):
    docs: List[SystemCredsLiteDoc]


class SystemCredsDoc(IDN, SystemCredsDocCreate):
    __collection__ = "SystemCreds"

    model_config = ConfigDict(populate_by_name=True)
