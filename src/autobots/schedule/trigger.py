import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from src.autobots.data_model.timezone_enum import TimezoneEnum


class IntervalTriggerModel(BaseModel):
    trigger: Literal["interval"] = "interval"
    seconds: int = Field(86400, ge=3600)


class CronTriggerModel(BaseModel):
    """https://crontab.cronhub.io/"""

    trigger: Literal["cron"] = "cron"
    # second: int | str | None = Field(default=None, ge=0, le=59, description="second (0-59)")
    minute: int | str | None = Field(default=None, ge=0, le=59, description="minute (0-59)")
    hour: int | str | None = Field(default=None, ge=0, le=23, description="hour (0-23)")
    day: int | str | None = Field(default=None, ge=1, le=31, description="day of month (1-31)")
    day_of_week: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] | str | None = Field(
        default=None,
        description="number mapped name of weekday (0-6 or mon,tue,wed,thu,fri,sat,sun)",
    )
    week: int | str | None = Field(default=None, ge=1, le=53, description="ISO week (1-53)")
    month: int | str | None = Field(default=None, ge=1, le=12, description="month (1-12)")
    year: int | str | None = Field(default=None, ge=1970, description="4-digit year")
    start_date: datetime.datetime | None = Field(
        default=None, description="earliest possible date/time to trigger on (inclusive)"
    )
    end_date: datetime.datetime | None = Field(
        default=None, description="latest possible date/time to trigger on (inclusive)"
    )
    timezone: TimezoneEnum | None = Field(
        default=None,
        description="time zone to use for the date/time calculations (defaults to scheduler timezone)",
    )  # datetime.tzinfo
    jitter: int | None = Field(
        default=None, ge=0, description="delay the job execution by ``jitter`` seconds at most"
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_serializer("start_date", "end_date", when_used="json")
    def serialize_datetime(self, value: datetime.datetime | None) -> str | None:
        """Serialize datetime objects to ISO format strings for JSON serialization"""
        if value is None:
            return None
        return value.isoformat()

    """
    :param int|str year: 4-digit year
    :param int|str month: month (1-12)
    :param int|str day: day of month (1-31)
    :param int|str week: ISO week (1-53)
    :param int|str day_of_week: number or name of weekday (0-6 or mon,tue,wed,thu,fri,sat,sun)
    :param int|str hour: hour (0-23)
    :param int|str minute: minute (0-59)
    :param int|str second: second (0-59)
    :param datetime|str start_date: earliest possible date/time to trigger on (inclusive)
    :param datetime|str end_date: latest possible date/time to trigger on (inclusive)
    :param datetime.tzinfo|str timezone: time zone to use for the date/time calculations (defaults
        to scheduler timezone)
    :param int|None jitter: delay the job execution by ``jitter`` seconds at most
    """


# Trigger = Union[IntervalTrigger, CronTriggerModel]
# Trigger = Union[IntervalTrigger, CronTrigger]
