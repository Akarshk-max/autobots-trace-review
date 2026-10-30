from typing import Optional

from pydantic import BaseModel, ConfigDict

from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.id import ID
from src.autobots.data_model.MongoId import MongoID
from src.autobots.data_model.space_id import OptionalSpaceId
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId
from src.autobots.event_result.event_result_model import EventType


class PodCreate(BaseModel):
    current_runnable_type: Optional[EventType] = None
    current_runnable_id: Optional[str] = None
    current_run_id: Optional[str] = None


class PodDocCreate(UserId, OptionalSpaceId, PodCreate, CreatedAt, UpdatedAt):
    pass


class PodDocFind(OptionalSpaceId):
    user_id: str | None = None


class PodUpdate(PodCreate):
    pass


class PodDocUpdate(ID, UserId, PodUpdate, UpdatedAt):
    pass


class PodDoc(MongoID, PodDocCreate):
    __collection__ = "Pods"

    model_config = ConfigDict(populate_by_name=True, extra="ignore")
