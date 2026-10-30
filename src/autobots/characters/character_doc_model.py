from typing import List, Union

from pydantic import BaseModel, ConfigDict

from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.id import ID, OptionalID
from src.autobots.data_model.MongoId import MongoID
from src.autobots.data_model.space_id import OptionalSpaceId
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId


class CharacterCreate(BaseModel):
    name: str
    tags: List[str] = []
    is_published: bool = False


class CharacterDocCreate(UserId, OptionalSpaceId, CharacterCreate, CreatedAt, UpdatedAt):
    pass


class CharacterRead(OptionalID):
    name: str | None = None
    tags: List[str] | None = None
    is_published: bool | None = None


class CharacterDocRead(UserId, OptionalSpaceId, CharacterRead):
    pass


class PublishedCharacterDocRead(CharacterRead):
    is_published: bool = True


# Using RootModel to create a union of CharacterDocRead and PublishedCharacterDocRead
# class CharacterDocReads(RootModel[Union[CharacterDocRead, PublishedCharacterDocRead]]):
#     pass
CharacterDocReads = Union[CharacterDocRead, PublishedCharacterDocRead]


class CharacterUpdate(CharacterCreate):
    name: str | None = None
    tags: List[str] | None = None
    is_published: bool | None = None


class CharacterDocUpdate(ID, UserId, CharacterUpdate, CreatedAt, UpdatedAt):
    pass


class CharacterDoc(MongoID, CharacterDocCreate):
    __collection__ = "Characters"

    model_config = ConfigDict(populate_by_name=True)


class CharacterLiteDoc(CharacterDoc):
    pass


class CharacterLiteDocPage(DocFindPage):
    docs: List[CharacterLiteDoc]
