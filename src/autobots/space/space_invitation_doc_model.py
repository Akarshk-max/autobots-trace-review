from datetime import datetime, timedelta
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic_extra_types.pendulum_dt import DateTime

from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.space.space_doc_model import SpaceMemberRole

INVITATION_EXPIRY_DAYS = 7


class InvitationStatus(str, Enum):
    pending = "pending"
    accepted = "accepted"
    declined = "declined"
    expired = "expired"


class SpaceInvitationCreate(BaseModel):
    invitee_email: str
    role: SpaceMemberRole = SpaceMemberRole.member


class SpaceInvitationFind(BaseModel):
    id: Optional[str] = Field(default=None)
    space_id: Optional[str] = None
    invitee_email: Optional[str] = None
    status: Optional[InvitationStatus] = None
    invited_by: Optional[str] = None


class SpaceInvitationDocFind(SpaceInvitationFind):
    user_id: str | None = None


class SpaceInvitationDocCreate(CreatedAt, UpdatedAt):
    space_id: str
    invitee_email: str
    role: SpaceMemberRole
    invited_by: str
    status: InvitationStatus = InvitationStatus.pending
    expires_at: datetime = Field(
        default_factory=lambda: datetime.now() + timedelta(days=INVITATION_EXPIRY_DAYS)
    )


class SpaceInvitationDocUpdate(UpdatedAt):
    id: str
    user_id: str  # required by CRUDBase.update_one — set to invited_by
    status: Optional[InvitationStatus] = None


class SpaceInvitationLiteDoc(BaseModel):
    id: str = Field(..., alias="_id")
    space_id: str
    invitee_email: str
    role: SpaceMemberRole
    invited_by: str
    status: InvitationStatus
    created_at: datetime
    updated_at: Optional[DateTime] = None
    expires_at: datetime

    model_config = ConfigDict(populate_by_name=True)


class SpaceInvitationDoc(SpaceInvitationDocCreate):
    __collection__ = "SpaceInvitations"

    id: str = Field(..., alias="_id")

    model_config = ConfigDict(populate_by_name=True)


class SpaceInvitationDocsFound(DocFindPage):
    docs: List[SpaceInvitationLiteDoc]
