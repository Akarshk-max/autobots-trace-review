"""A SELECT runs off the event loop, carries the run's cancellation token, and stops early."""

from __future__ import annotations

import asyncio

import pytest

from src.autobots.action.action_type.action_text2text.action_sql_runner import action_sql_runner
from src.autobots.action.action_type.action_text2text.action_sql_runner.action_sql_runner import (
    ActionSqlRunner,
    DatabaseEnum,
    _SqlRunnerInternalConfig,
)
from src.autobots.core.cancellation.run_cancellation import RunCancellation, RunCancelledError
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context


def _config(sql: str = "select 1") -> _SqlRunnerInternalConfig:
    return _SqlRunnerInternalConfig(
        database_type=DatabaseEnum.sqlite, database=":memory:", sql_query=sql
    )


def _app_state(cancelled: bool = False) -> AppState:
    app_state = AppState()
    cancellation = RunCancellation(run_id="run-1")
    if cancelled:
        cancellation.cancel("stop")
    app_state.set_cancellation(cancellation)
    return app_state


@pytest.mark.asyncio
async def test_non_select_is_rejected_before_connecting(monkeypatch):
    called = False

    def _never(*a, **k):
        nonlocal called
        called = True

    monkeypatch.setattr(ActionSqlRunner, "_execute_select_query", staticmethod(_never))

    result = await ActionSqlRunner.run_select_query(
        ctx=Context(user_id="u1"), internal_config=_config("delete from t"), app_state=_app_state()
    )

    assert isinstance(result, Exception)
    assert not called


@pytest.mark.asyncio
async def test_cancellation_token_reaches_the_query(monkeypatch):
    seen = {}

    def _capture(ctx, internal_config, cancellation):
        seen["cancellation"] = cancellation
        from src.autobots.action.action.common_action_models import DataBlocks

        return DataBlocks()

    monkeypatch.setattr(ActionSqlRunner, "_execute_select_query", staticmethod(_capture))
    app_state = _app_state()

    await ActionSqlRunner.run_select_query(
        ctx=Context(user_id="u1"), internal_config=_config(), app_state=app_state
    )

    assert seen["cancellation"] is app_state.cancellation


@pytest.mark.asyncio
async def test_already_cancelled_run_never_opens_a_connection(monkeypatch):
    called = False

    def _never(*a, **k):
        nonlocal called
        called = True

    monkeypatch.setattr(ActionSqlRunner, "_execute_select_query", staticmethod(_never))

    # Cancellation is a BaseException, so it propagates rather than being returned as
    # an error - that is what lets the activity record the run as cancelled, not failed.
    with pytest.raises(RunCancelledError):
        await ActionSqlRunner.run_select_query(
            ctx=Context(user_id="u1"),
            internal_config=_config(),
            app_state=_app_state(cancelled=True),
        )

    assert not called


@pytest.mark.asyncio
async def test_query_runs_off_the_event_loop(monkeypatch):
    """The blocking body must not hold the loop, or the activity heartbeat stalls."""
    ticks = 0

    def _slow(ctx, internal_config, cancellation):
        import time

        time.sleep(0.4)
        from src.autobots.action.action.common_action_models import DataBlocks

        return DataBlocks()

    monkeypatch.setattr(ActionSqlRunner, "_execute_select_query", staticmethod(_slow))

    async def _heartbeat():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.05)

    beat = asyncio.create_task(_heartbeat())
    await ActionSqlRunner.run_select_query(
        ctx=Context(user_id="u1"), internal_config=_config(), app_state=_app_state()
    )
    beat.cancel()

    assert ticks > 3, f"event loop was blocked; only {ticks} ticks"


def test_driver_cancel_helper_is_the_shared_one():
    from src.autobots.action.action_type.action_text2text.action_sql_runner.action_db_sql_runner import (
        register_query_cancel,
    )

    assert action_sql_runner.register_query_cancel is register_query_cancel
