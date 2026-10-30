import asyncio
import typing

from loguru import logger
from pydantic import BaseModel, Field

from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState


class SleepObj(BaseModel):
    sleep_sec: int = Field(60, ge=30, le=86400)


async def sleep(
    config: SleepObj, ctx: Context, app_state: AppState, notify: Notify | None = None
) -> typing.AnyStr | Exception:
    """
    Description
    -----------
    Suspends execution for the given number of seconds and then returns a
    confirmation message. The sleep duration must fall within the allowed
    range. If an unexpected error occurs, the exception is returned.

    Input
    -----
    config : dict
        A dictionary of arguments supplied by the caller.
        Expected structure:
            {
                "sleep_sec": 60
            }

        sleep_sec : int
            The number of seconds to sleep. Must be between 30 and 86400
            (inclusive). Defaults to 60 if not provided.

    Output
    ------
    str
        A message indicating how long the function slept.
        In case of an unexpected error, the exception object is returned.
    """
    try:
        await asyncio.sleep(config.sleep_sec)
        return f"slept for {config.sleep_sec} seconds"
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in sleep: {str(e)}")
        return e
