from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.autobots.data_model.space_id import OptionalSpaceId
from src.autobots.data_model.user_id import UserId
from src.autobots.event_result.event_result_model import EventResultStatus, EventType

TERMINAL_STATUSES = {
    EventResultStatus.success,
    EventResultStatus.error,
    EventResultStatus.cancelled,
}


class RunTrigger(str, Enum):
    api = "api"
    webhook = "webhook"
    scheduled = "scheduled"
    batch = "batch"
    unknown = "unknown"


class UnitKind(str, Enum):
    file = "file"
    row = "row"
    text = "text"
    api_batch = "api_batch"
    single = "single"


class NodeUnits(BaseModel):
    node_id: str
    kind: UnitKind = UnitKind.single
    total: int = 0
    succeeded: int = 0
    rate_per_unit: Optional[float] = None
    hours_saved: Optional[float] = None


class RunTokenTotals(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cached: int = 0
    tokens_cache_write: int = 0
    tokens_reasoning: int = 0
    tokens_total: int = 0
    llm_cost_usd: Optional[float] = None
    llm_cost_complete: bool = False


class RunModelTotals(RunTokenTotals):
    """One model's share of a run. A run that touched three models carries three of these,
    so spend is reported against the model that actually incurred it."""

    model: str


class RunMetricsDocCreate(UserId, OptionalSpaceId, RunTokenTotals):
    is_root_run: bool = Field(
        default=True,
        description="False for a run executed inside another run, such as a team's delegated "
        "member. Its tokens and cost are already counted in the parent, so the dashboard "
        "aggregates root runs only and keeps these for a per-action drill-down.",
    )
    type: EventType
    status: EventResultStatus
    created_at: datetime
    finished_at: datetime
    subject_id: Optional[str] = None

    units_total: int = 1
    units_succeeded: int = 1
    units_by_node: List[NodeUnits] = Field(default_factory=list)
    hours_saved: Optional[float] = None
    files_in: int = 0
    files_out: int = 0
    duration_seconds: Optional[float] = None
    trigger: RunTrigger = RunTrigger.unknown
    error_class: Optional[str] = None

    cost_per_hour: Optional[float] = None

    totals_by_model: List[RunModelTotals] = Field(default_factory=list)


class RunMetricsDoc(RunMetricsDocCreate):
    __collection__ = "RunMetrics"

    id: str = Field(..., alias="_id")

    model_config = ConfigDict(populate_by_name=True)


class RunMetricsDocFind(BaseModel):
    id: Optional[str] = None
    user_id: Optional[str] = None
    space_id: Optional[str] = None
    type: Optional[EventType] = None
    status: Optional[EventResultStatus] = None
    subject_id: Optional[str] = None
    trigger: Optional[RunTrigger] = None


class RunMetricsDocUpdate(BaseModel):
    id: str
    user_id: str
