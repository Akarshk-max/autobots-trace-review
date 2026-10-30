"""A tool run is a stored record: created before dispatch, readable afterwards, never lost."""

from __future__ import annotations

import asyncio

import pytest

from src.autobots.core.settings import SettingsProvider
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.llm.tools import tool_service
from src.autobots.llm.tools.tool_result.tool_result_doc_model import ToolResultUpdate, ToolRun
from src.autobots.llm.tools.tool_result.user_tool_result import UserToolResult
from src.autobots.llm.tools.tool_service import ToolNotFound, ToolRunNotFound, ToolService


def _client(captured: dict, on_result=None):
    class _Handle:
        async def result(self):
            if on_result is None:
                await asyncio.sleep(30)
            return await on_result(captured["payload"])

    class _Client:
        async def start_workflow(self, workflow, payload, *, id, task_queue):
            captured["payload"] = payload
            captured["id"] = id
            captured["task_queue"] = task_queue
            return _Handle()

    async def get_client():
        return _Client()

    return get_client


@pytest.fixture
def no_inline_wait(monkeypatch):
    monkeypatch.setattr(SettingsProvider.sget(), "TOOL_RUN_INLINE_WAIT_SECONDS", 0.2)


@pytest.mark.asyncio
async def test_slow_tool_returns_a_processing_record(
    ctx, db, user, monkeypatch, set_test_settings, no_inline_wait
):
    captured: dict = {}
    monkeypatch.setattr(tool_service, "get_temporal_client", _client(captured))

    doc = await ToolService.run_tool(
        ctx=ctx, user=user, db=db, tool_name="get_date_time", tool_args={"timezone": "UTC"}
    )

    assert doc.status == EventResultStatus.processing
    assert doc.result.tool_name == "get_date_time"
    assert doc.result.output is None
    assert captured["payload"].tool_run_id == doc.id
    assert captured["id"] == f"tool-{doc.id}"
    assert captured["task_queue"] == SettingsProvider.sget().TEMPORAL_TASK_QUEUE


@pytest.mark.asyncio
async def test_a_processing_run_is_retrievable_by_id(
    ctx, db, user, monkeypatch, set_test_settings, no_inline_wait
):
    monkeypatch.setattr(tool_service, "get_temporal_client", _client({}))

    doc = await ToolService.run_tool(
        ctx=ctx, user=user, db=db, tool_name="get_date_time", tool_args={}
    )
    fetched = await ToolService.get_tool_run(user=user, db=db, tool_run_id=doc.id)

    assert fetched.id == doc.id
    assert fetched.result.tool_name == "get_date_time"


@pytest.mark.asyncio
async def test_fast_tool_returns_its_output_inline(
    ctx, db, user, monkeypatch, set_test_settings
):
    async def finish(payload):
        await UserToolResult(user, db).update_tool_result(
            payload.tool_run_id,
            ToolResultUpdate(
                status=EventResultStatus.success,
                result=ToolRun(
                    tool_name=payload.tool_name,
                    tool_args=payload.tool_args,
                    output={"text": "2026-08-29", "urls": [], "file_ids": []},
                ),
            ),
        )
        return {}

    monkeypatch.setattr(tool_service, "get_temporal_client", _client({}, on_result=finish))

    doc = await ToolService.run_tool(
        ctx=ctx, user=user, db=db, tool_name="get_date_time", tool_args={}
    )

    assert doc.status == EventResultStatus.success
    assert doc.result.output["text"] == "2026-08-29"


@pytest.mark.asyncio
async def test_webhook_is_carried_to_the_runner(
    ctx, db, user, monkeypatch, set_test_settings, no_inline_wait
):
    captured: dict = {}
    monkeypatch.setattr(tool_service, "get_temporal_client", _client(captured))

    await ToolService.run_tool(
        ctx=ctx,
        user=user,
        db=db,
        tool_name="get_date_time",
        tool_args={},
        webhook={"url": "https://example.com/hook", "track_id": "t1"},
    )

    assert captured["payload"].webhook["url"] == "https://example.com/hook"


@pytest.mark.asyncio
async def test_unknown_tool_creates_no_record(ctx, db, user, monkeypatch, set_test_settings):
    captured: dict = {}
    monkeypatch.setattr(tool_service, "get_temporal_client", _client(captured))

    with pytest.raises(ToolNotFound):
        await ToolService.run_tool(
            ctx=ctx, user=user, db=db, tool_name="no_such_tool", tool_args={}
        )
    assert captured == {}


@pytest.mark.asyncio
async def test_unknown_run_id_is_reported(db, user, set_test_settings):
    with pytest.raises(ToolRunNotFound):
        await ToolService.get_tool_run(user=user, db=db, tool_run_id="6710000000000000000000aa")
