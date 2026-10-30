from typing import List, Union

from pydantic import BaseModel, ConfigDict

from src.autobots.action.action.common_action_models import DataBlocks
from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.id import ID, OptionalID
from src.autobots.data_model.MongoId import MongoID
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId


class PromptCreate(BaseModel):
    name: str
    tags: List[str] = []
    is_published: bool = False
    blocks: DataBlocks


class PromptDocCreate(UserId, PromptCreate, CreatedAt, UpdatedAt):
    pass


class PromptRead(OptionalID):
    name: str | None = None
    tags: List[str] | None = None
    is_published: bool | None = None


class PromptDocRead(UserId, PromptRead):
    pass


class PublishedPromptDocRead(PromptRead):
    is_published: bool = True


PromptDocReads = Union[PromptDocRead, PublishedPromptDocRead]


class PromptUpdate(BaseModel):
    name: str | None = None
    tags: List[str] | None = None
    is_published: bool | None = None
    blocks: DataBlocks | None = None


class PromptDocUpdate(ID, UserId, PromptUpdate, CreatedAt, UpdatedAt):
    pass


class PromptDoc(MongoID, PromptDocCreate):
    __collection__ = "Prompts"
    model_config = ConfigDict(populate_by_name=True)


class PromptLiteDoc(PromptDoc):
    pass


class PromptLiteDocPage(DocFindPage):
    docs: List[PromptLiteDoc]