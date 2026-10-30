from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_extra_types.pendulum_dt import DateTime

from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.space_id import OptionalSpaceId, SpaceId
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId


class TeamMemberRole(str, Enum):
    leader = "leader"
    member = "member"


class ThreadEntryRole(str, Enum):
    user = "user"
    leader = "leader"
    member = "member"


class ThreadEntry(BaseModel):
    member_id: str | None = None
    action_id: str = ""
    action_run_id: str | None = None
    name: str = ""
    role: ThreadEntryRole
    input: Optional[Dict[str, Any]] = None
    output: Optional[Dict[str, Any]] = None


class TeamMember(BaseModel):
    member_id: str | None = None
    action_id: str
    action_run_ids : List[str] | None = None
    role: TeamMemberRole = TeamMemberRole.member
    name: str = ""
    description: str = ""

class ActionTeamResult(BaseModel):
    input: Optional[Dict[str, Any]] = None
    output: Optional[Dict[str, Any]] = None
    
class ActionTeamFind(BaseModel):
    id: Optional[str] = Field(default=None)
    name: Optional[str] = None
    version: Optional[float] = None
    description: Optional[str] = None
    is_saved: Optional[bool] = None
    time_saved_per_run: Optional[float] = Field(
        default=None,
        description="Hours a person would spend doing one run of this team by hand.",
    )
    dollar_cost_per_hour: Optional[float] = Field(
        default=None,
        description="Dollar cost of an hour of that person's time.",
    )
    is_published: Optional[bool] = None
    shared_with: Optional[SpaceId | str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ActionTeamDocFind(ActionTeamFind, OptionalSpaceId):
    user_id: str | None = None


class ActionTeamUpdate(BaseModel):
    name: Optional[str] = None
    version: Optional[float] = None
    description: Optional[str] = None
    user_manual: Optional[str] = None
    members: Optional[List[TeamMember]] = None
    max_iterations: Optional[int] = None
    is_saved: Optional[bool] = None
    time_saved_per_run: Optional[float] = Field(
        default=None,
        description="Hours a person would spend doing one run of this team by hand.",
    )
    dollar_cost_per_hour: Optional[float] = Field(
        default=None,
        description="Dollar cost of an hour of that person's time.",
    )
    is_published: Optional[bool] = None
    shared_with: Optional[List[SpaceId | str]] = None


class ActionTeamDocUpdate(ActionTeamUpdate, UpdatedAt):
    id: str
    user_id: str


class ActionTeamCreate(BaseModel):
    name: str
    version: float = 0
    description: str = ""
    user_manual: str = ""
    members: List[TeamMember]
    max_iterations: int = 50
    is_saved: bool = False
    time_saved_per_run: Optional[float] = Field(
        default=0.5,
        description="Hours a person would spend doing one run of this team by hand.",
    )
    dollar_cost_per_hour: Optional[float] = Field(
        default=50,
        description="Dollar cost of an hour of that person's time.",
    )
    is_published: bool = False
    shared_with: List[SpaceId | str] = []

    @model_validator(mode="after")
    def validate_exactly_one_leader(self):
        leaders = [m for m in self.members if m.role == TeamMemberRole.leader]
        if len(leaders) != 1:
            raise ValueError(f"Exactly one member must have role 'leader', found {len(leaders)}")
        return self


class ActionTeamDocCreate(UserId, OptionalSpaceId, ActionTeamCreate, CreatedAt, UpdatedAt):
    pass


class ActionTeamLiteDoc(OptionalSpaceId, BaseModel):
    id: str = Field(..., alias="_id")
    name: str
    version: float = 0
    description: str = ""
    user_manual: str = ""
    members: List[TeamMember] = []
    max_iterations: int = 50
    is_saved: bool = False
    time_saved_per_run: Optional[float] = 0.5
    dollar_cost_per_hour: Optional[float] = 50
    is_published: bool = False
    shared_with: List[SpaceId | str] = []
    user_id: str
    created_at: datetime
    updated_at: Optional[DateTime] = None

    model_config = ConfigDict(populate_by_name=True)


class ActionTeamDoc(ActionTeamDocCreate):
    __collection__ = "ActionTeams"

    id: str = Field(..., alias="_id")
    input: Optional[Dict[str, Any]] = None
    output: Dict[str, Any] = {}
    thread: List[ThreadEntry] = []
    results: List[ActionTeamResult] = []
    current_active_members: List[Dict[str, str]] = [] # List[Member ID : Action Run ID]
    model_config = ConfigDict(populate_by_name=True)


class ActionTeamDocsFound(DocFindPage):
    docs: List[ActionTeamLiteDoc]
