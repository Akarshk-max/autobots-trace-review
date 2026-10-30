import asyncio

import pytest
from temporalio.testing import ActivityEnvironment

from src.autobots.conn.temporal.heartbeat import run_with_heartbeat
from src.autobots.core.cancellation.run_cancellation import RunCancellation, RunCancelledError


@pytest.mark.asyncio
async def test_activity_cancellation_sets_the_token_and_stops_the_work():
    env = ActivityEnvironment()
    token = RunCancellation(run_id="run1")
    started = asyncio.Event()
    completed = False

    async def long_running_work():
        nonlocal completed
        started.set()
        await asyncio.sleep(30)
        completed = True

    async def activity():
        return await run_with_heartbeat(long_running_work(), token)

    task = asyncio.create_task(env.run(activity))
    await asyncio.wait_for(started.wait(), timeout=5)
    env.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert token.is_cancelled is True
    assert completed is False


@pytest.mark.asyncio
async def test_activity_cancellation_fires_registered_driver_cancel():
    env = ActivityEnvironment()
    token = RunCancellation(run_id="run1")
    started = asyncio.Event()
    driver_cancelled = []

    token.register_driver_cancel(lambda: driver_cancelled.append("killed"))

    async def blocking_work():
        started.set()
        await asyncio.sleep(30)

    async def activity():
        return await run_with_heartbeat(blocking_work(), token)

    task = asyncio.create_task(env.run(activity))
    await asyncio.wait_for(started.wait(), timeout=5)
    env.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert driver_cancelled == ["killed"]


@pytest.mark.asyncio
async def test_cooperative_cancellation_propagates_out_of_the_activity():
    env = ActivityEnvironment()
    token = RunCancellation(run_id="run1")

    async def work_that_checks_its_token():
        token.cancel("user cancelled")
        token.raise_if_cancelled()

    async def activity():
        return await run_with_heartbeat(work_that_checks_its_token(), token)

    with pytest.raises(RunCancelledError):
        await env.run(activity)


@pytest.mark.asyncio
async def test_uncancelled_run_returns_its_result_and_heartbeats():
    env = ActivityEnvironment()
    heartbeats = []
    env.on_heartbeat = lambda *args: heartbeats.append(args)
    token = RunCancellation(run_id="run1")

    async def work():
        await asyncio.sleep(0.05)
        return {"status": "success"}

    async def activity():
        return await run_with_heartbeat(work(), token)

    assert await env.run(activity) == {"status": "success"}
    assert token.is_cancelled is False
    assert len(heartbeats) >= 1


@pytest.mark.asyncio
async def test_work_that_ignores_cancellation_does_not_stall_the_activity(monkeypatch):
    from src.autobots.core.settings import SettingsProvider

    settings = SettingsProvider.sget()
    monkeypatch.setattr(settings, "TEMPORAL_CANCEL_CLEANUP_TIMEOUT_SECONDS", 1, raising=False)

    env = ActivityEnvironment()
    token = RunCancellation(run_id="run1")
    started = asyncio.Event()

    async def work_with_a_hung_cleanup():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(30)
            raise

    async def activity():
        return await run_with_heartbeat(work_with_a_hung_cleanup(), token)

    task = asyncio.create_task(env.run(activity))
    await asyncio.wait_for(started.wait(), timeout=5)
    env.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    assert token.is_cancelled is True
