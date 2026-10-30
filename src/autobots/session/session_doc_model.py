from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.autobots.core.clock.clock import Clock
from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.space_id import OptionalSpaceId
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId
from src.autobots.share_data.share_data_doc_model import RunnableType


class SessionRunMapping(BaseModel):
    """Maps a runnable (action/graph/team) to its run"""

    runnable_id: str
    runnable_type: RunnableType
    run_id: Optional[str] = None


class SessionFind(BaseModel):
    """Input from User to find session"""

    id: Optional[str] = Field(default=None)
    name: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class SessionDocFind(SessionFind, OptionalSpaceId):
    """Add in user id to enforce multi-tenancy"""

    user_id: str | None = None


class SessionCreate(BaseModel):
    """Input from User to create session"""

    name: str = Field(
        default_factory=lambda: (
            f"{Clock.datetime_now().strftime('%Y-%m-%d')}-{str(gen_uuid())[:8]}"
        )
    )
    run_mappings: List[SessionRunMapping] = []


class SessionDocCreate(UserId, OptionalSpaceId, SessionCreate, CreatedAt, UpdatedAt):
    """Add in user id to enforce multi-tenancy"""

    pass


class SessionUpdate(BaseModel):
    """Input from User to update session"""

    name: Optional[str] = None
    run_mappings: Optional[List[SessionRunMapping]] = None


class SessionDocUpdate(SessionUpdate, UpdatedAt):
    id: str
    user_id: str


class SessionDoc(SessionDocCreate):
    __collection__ = "Sessions"

    id: str = Field(..., alias="_id")

    model_config = ConfigDict(populate_by_name=True)


class SessionDocsFound(DocFindPage):
    docs: List[SessionDoc]
