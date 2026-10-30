from typing import List

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.space_id import OptionalSpaceId
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId
from src.autobots.integrations.app_types import AppTypes


class ID(BaseModel):
    id: str = Field(..., alias="_id")


class OAuthCredsModel(BaseModel):
    client_id: str
    client_secret: str
    api_key: str
    scope: str


class OAuthCredsWithRedirectModel(OAuthCredsModel):
    redirect_uri: str


class ApiKeyCredsModel(BaseModel):
    api_key: str


class GrokApikeyCredsModel(BaseModel):
    api_key: str
    management_api_key: str = ""


class ApiKeyWithApiDomainCredsModel(ApiKeyCredsModel):
    api_domain: HttpUrl


class AppCredsCreate(BaseModel):
    app_name: str | AppTypes
    name: str
    default: bool = True
    creds: dict  # BaseModel | OAuthCredsModel | ApiKeyCredsModel | OAuthCredsWithRedirectModel


class AppCredsDocCreate(UserId, OptionalSpaceId, AppCredsCreate, CreatedAt, UpdatedAt):
    pass


class AppCredsRead(ID):
    id: str | None = None
    app_name: AppTypes | None = None
    name: str | None = None
    default: bool | None = None


class AppCredsDocRead(UserId, OptionalSpaceId, AppCredsRead):
    pass


class AppCredsUpdate(AppCredsCreate):
    default: bool | None = None


class AppCredsDocUpdate(ID, UserId, AppCredsUpdate):
    id: str


class AppCredsDeleteDoc(AppCredsDocRead):
    pass


class AppCredsLiteDoc(ID, UserId, OptionalSpaceId, CreatedAt, UpdatedAt):
    app_name: AppTypes
    name: str | None = None
    default: bool | None = None
    model_config = ConfigDict(populate_by_name=True)


class AppCredsLiteDocPage(DocFindPage):
    docs: List[AppCredsLiteDoc]


class AppCredsDoc(ID, AppCredsDocCreate):
    __collection__ = "AppCreds"

    model_config = ConfigDict(populate_by_name=True)
