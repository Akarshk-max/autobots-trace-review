from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.data_model.space_id import OptionalSpaceId
from src.autobots.schedule.trigger import CronTriggerModel, IntervalTriggerModel


class CronSchedule(CronTriggerModel):
    # second: int | None = Field(default=None, ge=0, le=59, description="second (0-59)")
    minute: int | None = Field(default=None, ge=0, le=59, description="minute (0-59)")
    hour: int | None = Field(default=None, ge=0, le=23, description="hour (0-23)")
    day: int | None = Field(default=None, ge=1, le=31, description="day of month (1-31)")
    day_of_week: (
        List[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]]
        | Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        | None
    ) = Field(
        default=None,
        description="number mapped name of weekday (0-6 or mon,tue,wed,thu,fri,sat,sun)",
    )
    week: int | None = Field(default=None, ge=1, le=53, description="ISO week (1-53)")
    month: int | None = Field(default=None, ge=1, le=12, description="month (1-12)")
    year: int | None = Field(default=None, ge=1970, description="4-digit year")
    minute_mode: Literal["at", "every"] = "at"
    hour_mode: Literal["at", "every"] = "at"
    day_mode: Literal["at", "every"] = "at"
    week_mode: Literal["at", "every"] = "at"
    month_mode: Literal["at", "every"] = "at"


class ScheduleFind(BaseModel):
    id: str | None = None
    name: str | None = None
    runnable_id: str | None = None
    input: DataBlock | None = None
    job_id: str | None = None


class ScheduleDocFind(ScheduleFind, OptionalSpaceId):
    user_id: str


class ScheduleUpdate(BaseModel):
    name: str | None = None
    trigger: CronSchedule | IntervalTriggerModel | None = None
    runnable_id: str | None = None
    input: DataBlock | None = None
    run_id: str | None = None
    # job_id: str | None = None


class ScheduleDocUpdate(ScheduleUpdate):
    id: str
    user_id: str
    job_id: str | None = None


class ScheduleCreate(BaseModel):
    name: str
    trigger: CronSchedule | IntervalTriggerModel
    runnable_id: str
    input: DataBlock
    run_id: str | None = None


class ScheduleDocCreate(OptionalSpaceId, ScheduleCreate):
    job_id: str
    user_id: str


class ScheduleDoc(ScheduleDocCreate):
    __collection__ = "runkai_schedules"

    id: str = Field(..., alias="_id")

    model_config = ConfigDict(populate_by_name=True)
