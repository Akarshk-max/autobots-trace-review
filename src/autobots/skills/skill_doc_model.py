from typing import List, Optional

from pydantic import BaseModel, ConfigDict

from src.autobots.action.action.common_action_models import DataBlocks
from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.id import ID, OptionalID
from src.autobots.data_model.MongoId import MongoID
from src.autobots.data_model.space_id import OptionalSpaceId, SpaceId
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId


class SkillCreate(BaseModel):
    name: str
    blocks: DataBlocks
    is_published: bool = False
    shared_with: List[SpaceId | str] = []


class SkillDocCreate(UserId, OptionalSpaceId, SkillCreate, CreatedAt, UpdatedAt):
    pass


class SkillRead(OptionalID):
    name: str | None = None
    is_published: Optional[bool] = None
    shared_with: Optional[SpaceId | str] = None


class SkillDocRead(UserId, OptionalSpaceId, SkillRead):
    pass


class SkillDocReadSystem(OptionalSpaceId, SkillRead):
    """Find skills without user_id constraint (for system skills)."""
    user_id: str | None = None


class SkillUpdate(BaseModel):
    name: str | None = None
    blocks: DataBlocks | None = None
    is_published: Optional[bool] = None
    shared_with: Optional[List[SpaceId | str]] = None


class SkillDocUpdate(ID, UserId, SkillUpdate, CreatedAt, UpdatedAt):
    pass


class SkillDoc(MongoID, SkillDocCreate):
    __collection__ = "Skills"
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class SkillLiteDoc(MongoID, UserId, OptionalSpaceId, CreatedAt, UpdatedAt):
    name: str
    is_published: bool = False
    shared_with: List[SpaceId | str] = []

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class SkillLiteDocPage(DocFindPage):
    docs: List[SkillLiteDoc]
    
class SkillDocPage(DocFindPage):
    docs: List[SkillDoc]
    