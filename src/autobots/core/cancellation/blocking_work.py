import asyncio
from typing import Any, Callable
from src.autobots.data_model.app_state import AppState


async def run_blocking(
    app_state: AppState | None,
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run blocking work off the event loop, stopping first if the run is cancelled.

    Blocking work on the event loop stalls the activity heartbeat, which both
    prevents cancellation being delivered and can trip the heartbeat timeout into
    a full retry of the action.
    """
    if app_state is not None:
        app_state.raise_if_cancelled()
    return await asyncio.to_thread(fn, *args, **kwargs)
