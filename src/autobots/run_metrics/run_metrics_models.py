from datetime import datetime, timedelta
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, computed_field


class TimeRange(str, Enum):
    last_7_days = "last_7_days"
    last_30_days = "last_30_days"
    last_90_days = "last_90_days"
    last_year = "last_year"

    def window(self, now: Optional[datetime] = None) -> tuple[datetime, datetime]:
        end = now or datetime.now()
        return end - timedelta(days=RANGE_DAYS[self]), end


RANGE_DAYS = {
    TimeRange.last_7_days: 7,
    TimeRange.last_30_days: 30,
    TimeRange.last_90_days: 90,
    TimeRange.last_year: 365,
}


class Totals(BaseModel):

    model_config = ConfigDict(protected_namespaces=())

    runs: int = 0
    succeeded: int = 0
    cancelled: int = 0
    hours_saved: float = 0
    money_saved: float = 0
    units_total: int = 0
    units_succeeded: int = 0
    files_in: int = 0
    files_out: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cached: int = 0
    tokens_cache_write: int = 0
    tokens_reasoning: int = 0
    tokens_total: int = 0
    llm_cost_usd: Optional[float] = None
    llm_cost_runs: int = 0
    llm_cost_complete: bool = False
    duration_p50: Optional[float] = None
    duration_p95: Optional[float] = None

    @computed_field
    @property
    def failed(self) -> int:
        """A cancellation is a decision, not a failure, so it is neither succeeded nor failed."""
        return self.runs - self.succeeded - self.cancelled

    @computed_field
    @property
    def success_rate(self) -> Optional[float]:
        """Measured over the runs that were allowed to finish."""
        finished = self.runs - self.cancelled
        return self.succeeded / finished if finished else None

    @computed_field
    @property
    def cache_hit_rate(self) -> Optional[float]:
        billed_input = self.tokens_input + self.tokens_cached
        return self.tokens_cached / billed_input if billed_input else None

    @computed_field
    @property
    def cost_per_item(self) -> Optional[float]:
        if not self.llm_cost_complete or self.llm_cost_usd is None or not self.units_succeeded:
            return None
        return self.llm_cost_usd / self.units_succeeded

    @computed_field
    @property
    def tokens_per_item(self) -> Optional[float]:
        return self.tokens_total / self.units_succeeded if self.units_succeeded else None

    @computed_field
    @property
    def net_value(self) -> Optional[float]:
        if not self.llm_cost_complete or self.llm_cost_usd is None:
            return None
        return self.money_saved - self.llm_cost_usd

    @computed_field
    @property
    def value_per_dollar(self) -> Optional[float]:
        """Estimated value against measured spend. Deliberately a multiple rather than a
        percentage: LLM spend is not the investment, so a ratio against it reads as a
        four-digit ROI that nobody believes."""
        if not self.llm_cost_complete or not self.llm_cost_usd:
            return None
        return self.money_saved / self.llm_cost_usd


class Bucket(Totals):
    bucket: datetime


class ModelTotals(BaseModel):
    """What a model can be held responsible for. Hours saved, money saved, units and files
    belong to the run, not to whichever model it happened to call."""

    model_config = ConfigDict(protected_namespaces=())

    model: Optional[str] = None
    runs: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cached: int = 0
    tokens_cache_write: int = 0
    tokens_reasoning: int = 0
    tokens_total: int = 0
    llm_cost_usd: Optional[float] = None
    llm_cost_runs: int = 0
    llm_cost_complete: bool = False


class SubjectTotals(Totals):
    subject_id: Optional[str] = None
    name: Optional[str] = None


class TriggerTotals(Totals):
    trigger: Optional[str] = None


class ErrorClassTotals(Totals):
    error_class: Optional[str] = None


class DashboardResponse(BaseModel):
    scope: str
    time_range: TimeRange
    start: datetime
    end: datetime
    timezone: str
    totals: Totals
    previous: Totals
    buckets: List[Bucket]
    top_subjects: List[SubjectTotals]
    by_trigger: List[TriggerTotals]
    by_model: List[ModelTotals]
    top_errors: List[ErrorClassTotals]
