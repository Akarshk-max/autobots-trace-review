import asyncio
import threading

import pytest

from src.autobots.core.cancellation.blocking_work import run_blocking
from src.autobots.core.cancellation.run_cancellation import RunCancellation, RunCancelledError
from src.autobots.data_model.app_state import AppState


def _app_state_with_token() -> tuple[AppState, RunCancellation]:
    app_state = AppState()
    cancellation = RunCancellation(run_id="run-1")
    app_state.set_cancellation(cancellation)
    return app_state, cancellation


@pytest.mark.asyncio
async def test_run_blocking_returns_result_and_passes_arguments():
    app_state, _ = _app_state_with_token()

    result = await run_blocking(app_state, lambda a, b=0: a + b, 2, b=3)

    assert result == 5


@pytest.mark.asyncio
async def test_run_blocking_without_app_state_still_runs():
    assert await run_blocking(None, lambda: "ok") == "ok"


@pytest.mark.asyncio
async def test_run_blocking_raises_and_skips_work_when_already_cancelled():
    app_state, cancellation = _app_state_with_token()
    cancellation.cancel("stop")
    called = False

    def work():
        nonlocal called
        called = True

    with pytest.raises(RunCancelledError):
        await run_blocking(app_state, work)

    assert called is False


@pytest.mark.asyncio
async def test_run_blocking_runs_off_the_event_loop():
    app_state, _ = _app_state_with_token()
    loop_thread = threading.current_thread().ident

    worker_thread = await run_blocking(app_state, lambda: threading.current_thread().ident)

    assert worker_thread != loop_thread


@pytest.mark.asyncio
async def test_event_loop_stays_responsive_while_blocking_work_runs():
    """The heartbeat must keep firing during blocking work, or cancellation cannot arrive."""
    app_state, _ = _app_state_with_token()
    started = threading.Event()
    release = threading.Event()
    ticks = 0

    def blocking():
        started.set()
        release.wait(timeout=5)
        return "done"

    async def tick():
        nonlocal ticks
        while not release.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    ticker = asyncio.ensure_future(tick())
    work = asyncio.ensure_future(run_blocking(app_state, blocking))

    assert await asyncio.to_thread(started.wait, 5)
    await asyncio.sleep(0.1)
    release.set()

    assert await work == "done"
    await ticker
    assert ticks > 1


@pytest.mark.asyncio
async def test_cancelling_the_awaiting_task_does_not_wait_for_the_thread():
    app_state, cancellation = _app_state_with_token()
    release = threading.Event()

    def blocking():
        release.wait(timeout=5)

    task = asyncio.ensure_future(run_blocking(app_state, blocking))
    await asyncio.sleep(0.05)
    cancellation.cancel("stop")
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    release.set()
