from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic_extra_types.pendulum_dt import DateTime

from src.autobots.action.action_type.action_types import ActionType
from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.space_id import OptionalSpaceId, SpaceId
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId


class ActionFind(BaseModel):
    """
    Input from User to find action
    """

    id: Optional[str] = Field(default=None)  # , alias='_id')
    name: Optional[str] = None
    version: Optional[float] = None
    description: Optional[str] = None
    type: Optional[ActionType] = None
    config: Optional[Dict[str, Any]] = None
    is_saved: Optional[bool] = None
    is_published: Optional[bool] = None
    shared_with: Optional[SpaceId | str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ActionDocFind(ActionFind, OptionalSpaceId):
    """
    Add in user id to enforce multi-tenancy
    """

    user_id: str | None = None


class ActionUpdate(BaseModel):
    name: Optional[str] = None
    version: Optional[float] = None
    description: Optional[str] = None
    user_manual: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    is_saved: Optional[bool] = None
    is_published: Optional[bool] = None
    shared_with: Optional[List[SpaceId | str]] = None
    time_saved_per_run: Optional[float] = Field(
        default=None,
        description="Time saved per run in hours for dashboard agent calculations",
    )
    dollar_cost_per_hour: Optional[float] = Field(
        default=None,
        description="Dollar cost per hour for dashboard agent calculations",
    )


class ActionDocUpdate(ActionUpdate, UpdatedAt):
    id: str
    user_id: str


class ActionResult(BaseModel):
    input: Optional[Dict[str, Any]] = None
    output: Optional[Dict[str, Any]] = None


class ActionCreate(BaseModel):
    """
    base class to be utilized by concrete action types to create action
    """

    name: str
    version: float = 0
    description: str = ""
    user_manual: str = ""
    type: ActionType
    config: Dict[str, Any] = {}
    is_saved: bool = False
    results: List[ActionResult] | None = []
    input: Optional[Dict[str, Any]] = None
    output: Optional[Dict[str, Any]] = None
    is_published: bool = False
    shared_with: List[SpaceId | str] = []
    time_saved_per_run: Optional[float] = Field(
        default=0.5,
        description="Time saved per run in hours for dashboard agent calculations",
    )
    dollar_cost_per_hour: Optional[float] = Field(
        default=50,
        description="Dollar cost per hour for dashboard agent calculations",
    )


class ActionDocCreate(UserId, OptionalSpaceId, ActionCreate, CreatedAt, UpdatedAt):
    """
    Add in user id to enforce multi-tenancy
    """

    pass


class ActionLiteDoc(OptionalSpaceId, BaseModel):
    id: str = Field(..., alias="_id")
    type: ActionType
    name: str
    version: float = 0
    user_manual: str = ""
    description: str = ""
    is_saved: bool = False
    is_published: bool = False
    shared_with: List[SpaceId | str] = []
    user_id: str
    created_at: datetime
    updated_at: Optional[DateTime] = None
    time_saved_per_run: Optional[float] = 0.5
    dollar_cost_per_hour: Optional[float] = 50

    model_config = ConfigDict(populate_by_name=True)


class ActionDoc(ActionDocCreate):
    __collection__ = "Actions"

    id: str = Field(..., alias="_id")

    model_config = ConfigDict(populate_by_name=True)


class ActionDocsFound(DocFindPage):
    docs: List[ActionLiteDoc]
