from typing import List

from pydantic import BaseModel, ConfigDict

from src.autobots.integrations.app_types import ExtendedEnum
from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.id import ID
from src.autobots.data_model.MongoId import MongoID
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId


class RunnableType(ExtendedEnum):
    action = "action"
    action_graph = "action_graph"
    action_team = "action_team"


class ShareDataCreate(BaseModel):
    data_id: str
    runnable_type: RunnableType


class ShareDataDocCreate(UserId, ShareDataCreate, CreatedAt, UpdatedAt):
    pass


class ShareDataRead(ID):
    id: str | None = None
    data_id: str | None = None
    runnable_type: RunnableType | None = None


class ShareDataDocRead(UserId, ShareDataRead):
    user_id: str | None = None


class ShareDataUpdate(ShareDataCreate):
    data_id: str | None = None
    runnable_type: RunnableType | None = None


class ShareDataDocUpdate(ID, UserId, ShareDataUpdate):
    pass


class ShareDataDeleteDoc(ShareDataRead):
    pass


class ShareDataDoc(MongoID, ShareDataDocCreate):
    __collection__ = "ShareData"

    model_config = ConfigDict(populate_by_name=True)


class ShareDataDocsPage(DocFindPage):
    docs: List[ShareDataDoc]
