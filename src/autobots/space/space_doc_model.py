from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic_extra_types.pendulum_dt import DateTime

from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId


class SpaceMemberRole(str, Enum):
    owner = "owner"
    admin = "admin"
    member = "member"

ADMIN_ROLES = {SpaceMemberRole.owner, SpaceMemberRole.admin}

# --- Space ---


class SpaceCreate(BaseModel):
    name: str
    description: str = ""


class SpaceFind(BaseModel):
    id: Optional[str] = Field(default=None)
    name: Optional[str] = None


class SpaceDocFind(SpaceFind):
    user_id: str | None = None


class SpaceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class SpaceDocUpdate(SpaceUpdate, UpdatedAt):
    id: str
    user_id: str


class SpaceDocCreate(UserId, SpaceCreate, CreatedAt, UpdatedAt):
    pass


class SpaceLiteDoc(BaseModel):
    id: str = Field(..., alias="_id")
    name: str
    description: str = ""
    user_id: str
    created_at: datetime
    updated_at: Optional[DateTime] = None

    model_config = ConfigDict(populate_by_name=True)


class SpaceDoc(SpaceDocCreate):
    __collection__ = "Spaces"

    id: str = Field(..., alias="_id")

    model_config = ConfigDict(populate_by_name=True)


class SpaceDocsFound(DocFindPage):
    docs: List[SpaceLiteDoc]


# --- Space Membership ---


class SpaceMemberCreate(BaseModel):
    space_id: str
    role: SpaceMemberRole = SpaceMemberRole.member


class SpaceMemberFind(BaseModel):
    id: Optional[str] = Field(default=None)
    space_id: Optional[str] = None
    role: Optional[SpaceMemberRole] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class SpaceMemberDocFind(SpaceMemberFind):
    user_id: str | None = None


class SpaceMemberUpdate(BaseModel):
    role: Optional[SpaceMemberRole] = None


class SpaceMemberDocUpdate(SpaceMemberUpdate, UpdatedAt):
    id: str
    user_id: str


class SpaceMemberDocCreate(UserId, CreatedAt, UpdatedAt):
    space_id: str
    role: SpaceMemberRole
    invited_by: str


class SpaceMemberLiteDoc(BaseModel):
    id: str = Field(..., alias="_id")
    space_id: str
    user_id: str
    role: SpaceMemberRole
    invited_by: str
    created_at: datetime
    updated_at: Optional[DateTime] = None

    model_config = ConfigDict(populate_by_name=True)


class SpaceMemberDoc(SpaceMemberDocCreate):
    __collection__ = "SpaceMembers"

    id: str = Field(..., alias="_id")

    model_config = ConfigDict(populate_by_name=True)


class SpaceMemberDocsFound(DocFindPage):
    docs: List[SpaceMemberLiteDoc]
