import asyncio

import pytest

from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.token_usage import token_usage_service
from src.autobots.token_usage.token_usage_service import LlmSurface, TokenUsageService
from src.autobots.user.user_orm_model import UserORM


class UnreachableDb:
    def __getitem__(self, name):
        raise RuntimeError("database is unreachable")


def _collected_app_state() -> AppState:
    app_state = AppState(action_id="action-1", action_run_id="run-1")
    TokenUsageService.start_collection(app_state)
    TokenUsageService.record(
        surface=LlmSurface.claude,
        ctx=Context(),
        app_state=app_state,
        usage={"input_tokens": 10, "output_tokens": 5},
        model="claude-opus-5",
        loop=1,
    )
    return app_state


class TestFlushNeverBreaksTheRun:
    @pytest.mark.asyncio
    async def test_a_failing_write_is_swallowed(self):
        app_state = _collected_app_state()

        await TokenUsageService.flush(
            ctx=Context(),
            app_state=app_state,
            user=UserORM(id="user-1"),
            db=UnreachableDb(),
            status=EventResultStatus.success,
        )

        assert app_state.token_usage_collector is None

    @pytest.mark.asyncio
    async def test_a_slow_write_is_dropped_rather_than_waited_on(self, monkeypatch):
        app_state = _collected_app_state()

        async def never_finishes(**_kwargs):
            await asyncio.sleep(60)

        monkeypatch.setattr(token_usage_service, "FLUSH_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(TokenUsageService, "_store", never_finishes)

        await asyncio.wait_for(
            TokenUsageService.flush(
                ctx=Context(),
                app_state=app_state,
                user=UserORM(id="user-1"),
                db=UnreachableDb(),
                status=EventResultStatus.cancelled,
            ),
            timeout=5,
        )

    @pytest.mark.asyncio
    async def test_a_cancelled_run_still_records_its_spend(self):
        app_state = _collected_app_state()
        stored = []

        async def capture(**kwargs):
            stored.extend(kwargs["doc_creates"])

        original = TokenUsageService._store
        TokenUsageService._store = capture
        try:
            await TokenUsageService.flush(
                ctx=Context(),
                app_state=app_state,
                user=UserORM(id="user-1"),
                db=UnreachableDb(),
                status=EventResultStatus.cancelled,
            )
        finally:
            TokenUsageService._store = original

        assert len(stored) == 1
        assert stored[0].status == EventResultStatus.cancelled
        assert stored[0].total_tokens == 15


class Exploding:
    """Stands in for a provider payload we have never seen before."""

    @property
    def prompt_tokens(self):
        raise RuntimeError("unexpected provider payload")

    def model_dump(self, **_kwargs):
        raise RuntimeError("unexpected provider payload")

    def get(self, *_args, **_kwargs):
        raise RuntimeError("unexpected provider payload")


class TestRecordingNeverBreaksTheRun:
    def test_a_hostile_payload_does_not_escape_any_provider(self):
        app_state = AppState(action_id="action-1", action_run_id="run-1")
        TokenUsageService.start_collection(app_state)

        for surface in LlmSurface:
            TokenUsageService.record(
                ctx=Context(),
                app_state=app_state,
                surface=surface,
                usage=Exploding(),
                model="some-model",
                loop=1,
            )

        assert app_state.token_usage_collector.is_empty()

    def test_a_broken_collector_does_not_escape(self):
        class BrokenCollector:
            def record(self, *_args, **_kwargs):
                raise RuntimeError("collector is broken")

        app_state = AppState(action_id="action-1", action_run_id="run-1")
        app_state.set_token_usage_collector(BrokenCollector())

        TokenUsageService.record(
            surface=LlmSurface.claude,
            ctx=Context(),
            app_state=app_state,
            usage={"input_tokens": 1, "output_tokens": 1},
            model="claude-opus-5",
            loop=1,
        )

    def test_recording_without_a_run_is_a_no_op(self):
        TokenUsageService.record(
            surface=LlmSurface.claude,
            ctx=Context(),
            app_state=None,
            usage={"input_tokens": 1, "output_tokens": 1},
            model="claude-opus-5",
            loop=1,
        )

    @pytest.mark.asyncio
    async def test_a_broken_collector_does_not_escape_the_flush(self):
        class BrokenCollector:
            def is_empty(self):
                raise RuntimeError("collector is broken")

        app_state = AppState(action_id="action-1", action_run_id="run-1")
        app_state.set_token_usage_collector(BrokenCollector())

        await TokenUsageService.flush(
            ctx=Context(),
            app_state=app_state,
            user=UserORM(id="user-1"),
            db=UnreachableDb(),
            status=EventResultStatus.success,
        )
