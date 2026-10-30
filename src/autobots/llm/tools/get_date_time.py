from loguru import logger
from pydantic import BaseModel

from src.autobots import SettingsProvider
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class TimeZoneObj(BaseModel):
    timezone: str = SettingsProvider.sget().TIMEZONE


class DateTimeObj(BaseModel):
    date_time: str


async def get_date_time(
    config: TimeZoneObj, ctx: Context, app_state: AppState, notify: Notify | None = None
) -> DateTimeObj | Exception:
    """
    Description
    -----------
    Returns the current date and time for the specified timezone. The output
    is formatted using a cookie-compatible timestamp string. If an unexpected
    error occurs, the exception is returned.

    Input
    -----
    config : dict
        A dictionary of arguments supplied by the caller (typically an LLM).
        Expected structure:
            {
                "timezone": "Asia/Kolkata"
            }

        timezone : str
            The IANA timezone identifier for which the current date and time
            should be returned. If not provided, a default timezone is used.

    Output
    ------
    DateTimeObj
        An object containing the formatted date-time string in its `date_time`
        field.

        In case of an unexpected error, the exception object is returned.
    """
    try:
        date_time = Clock.get_date_time(tz=config.timezone)
        return DateTimeObj(date_time=date_time.to_cookie_string())
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in get_date_time: {str(e)}")
        return e
