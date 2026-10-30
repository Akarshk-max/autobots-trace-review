from typing import List

from pydantic import BaseModel, ConfigDict

from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.MongoId import MongoID, OptionalMongoID
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId


class TeamCreate(BaseModel):
    name: str
    description: str = ""
    tags: List[str] = []
    leader: str


class TeamDocCreate(UserId, TeamCreate, CreatedAt, UpdatedAt):
    pass


class TeamRead(OptionalMongoID):
    name: str | None = None
    tags: List[str] | None = None


class TeamDocRead(UserId, TeamRead):
    pass


class TeamUpdate(TeamCreate):
    pass


class TeamDocUpdate(MongoID, UserId, TeamUpdate):
    pass


class TeamDoc(MongoID, TeamDocCreate):
    __collection__ = "Teams"

    model_config = ConfigDict(populate_by_name=True)


class TeamLiteDoc(TeamDoc):
    pass


class TeamLiteDocPage(DocFindPage):
    docs: List[TeamLiteDoc]
