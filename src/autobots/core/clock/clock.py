from datetime import datetime
from functools import lru_cache

import pendulum.tz
from pendulum import FixedTimezone, Timezone
from pydantic_extra_types.pendulum_dt import DateTime

from src.autobots import SettingsProvider


class Clock:
    @staticmethod
    @lru_cache
    def get_time_zone(timezone: str = SettingsProvider.sget().TIMEZONE) -> Timezone | FixedTimezone:
        return pendulum.timezone(timezone)

    @staticmethod
    def get_date_time(tz: str | None = None) -> DateTime:
        if not tz:
            return pendulum.now(Clock.get_time_zone())
        return pendulum.now(Clock.get_time_zone(tz))

    @staticmethod
    def datetime_now() -> datetime:
        return datetime.now()
