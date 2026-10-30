from typing import List, Literal

from pydantic import BaseModel, ConfigDict

from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.id import ID, OptionalID
from src.autobots.data_model.MongoId import MongoID
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId


class SlackInfo(BaseModel):
    slack_user_id: str
    slack_thread_id: str
    slack_channel_id: str


class SlackRunnableCreate(SlackInfo):
    runnable_type: Literal["action", "action_graph"]
    runnable_id: str
    run_id: str


class SlackRunnableDocCreate(UserId, CreatedAt, UpdatedAt, SlackRunnableCreate):
    pass


class SlackRunnableRead(BaseModel):
    slack_thread_id: str | None = None
    runnable_id: str | None = None
    run_id: str | None = None


class SlackRunnableDocRead(OptionalID, UserId, SlackRunnableRead):
    pass


class SlackRunnableUpdate(SlackRunnableCreate):
    pass


class SlackRunnableDocUpdate(ID, UserId, SlackRunnableUpdate, CreatedAt, UpdatedAt):
    pass


class SlackRunnableDoc(MongoID, SlackRunnableDocCreate):
    __collection__ = "slack_runnable"

    model_config = ConfigDict(populate_by_name=True)


class SlackRunnableLiteDoc(SlackRunnableDoc):
    pass


class SlackRunnableLiteDocPage(DocFindPage):
    docs: List[SlackRunnableLiteDoc]
