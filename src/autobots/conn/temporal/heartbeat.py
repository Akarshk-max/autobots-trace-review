import asyncio

from loguru import logger
from temporalio import activity

from src.autobots.core.cancellation.run_cancellation import RunCancellation
from src.autobots.core.settings import SettingsProvider


async def run_with_heartbeat(coro, cancellation: RunCancellation | None = None):
    """Run coro while heartbeating, bridging Temporal cancellation onto the token.

    activity.is_cancelled() stays set once cancellation is delivered, so polling it
    here keeps the token set even if downstream code absorbed the asyncio
    cancellation. The original exception is re-raised unchanged: asyncio
    cancellation is a BaseException that `except Exception` cannot swallow, and the
    SDK accepts it as a cancelled completion.
    """
    heartbeat_interval_seconds = SettingsProvider.sget().TEMPORAL_HEARTBEAT_INTERVAL_SECONDS
    task = asyncio.ensure_future(coro)
    try:
        while not task.done():
            activity.heartbeat("executing")
            if cancellation is not None and activity.is_cancelled():
                cancellation.cancel("Activity cancelled")
            await asyncio.wait({task}, timeout=heartbeat_interval_seconds)
        return task.result()
    except asyncio.CancelledError:
        if cancellation is not None:
            cancellation.cancel("Activity cancelled")
        task.cancel()
        cleanup_timeout = SettingsProvider.sget().TEMPORAL_CANCEL_CLEANUP_TIMEOUT_SECONDS
        _, pending = await asyncio.wait({task}, timeout=cleanup_timeout)
        if pending:
            logger.warning(
                f"Cancelled work did not unwind within {cleanup_timeout}s; abandoning it"
            )
        raise
