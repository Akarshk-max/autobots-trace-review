"""A run must never be left in processing: dispatch failures and dead activities both settle it."""

from __future__ import annotations

import uuid

import pytest

from src.autobots.conn.temporal.tool import activity as tool_activity
from src.autobots.conn.temporal.tool.shared import ToolFinalizeInput
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.llm.tools import tool_service
from src.autobots.llm.tools.tool_result.tool_result_doc_model import ToolResultCreate, ToolRun
from src.autobots.llm.tools.tool_result.user_tool_result import UserToolResult
from src.autobots.llm.tools.tool_service import ToolService


async def _make_run(user, db, status: EventResultStatus):
    return await UserToolResult(user, db).create_tool_result(
        ToolResultCreate(
            name="get_date_time",
            status=status,
            result=ToolRun(tool_name=f"probe_{uuid.uuid4().hex[:8]}", tool_args={}),
        )
    )


# --- Fix 1: dispatch failure ------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_failure_settles_the_record(ctx, db, user, monkeypatch, set_test_settings):
    async def _unreachable():
        raise ConnectionError("temporal unreachable")

    monkeypatch.setattr(tool_service, "get_temporal_client", _unreachable)

    doc = await ToolService.run_tool(
        ctx=ctx, user=user, db=db, tool_name="get_date_time", tool_args={}
    )

    assert doc.status == EventResultStatus.error
    assert "temporal unreachable" in doc.error_message.text
    # and it is not left processing for a workflow that will never exist
    fetched = await ToolService.get_tool_run(user=user, db=db, tool_run_id=doc.id)
    assert fetched.status == EventResultStatus.error


# --- Fix 2: finaliser -------------------------------------------------------------


@pytest.mark.asyncio
async def test_finaliser_marks_an_abandoned_run_stuck(db, user, monkeypatch, set_test_settings):
    doc = await _make_run(user, db, EventResultStatus.processing)
    monkeypatch.setattr(tool_activity, "get_mongo_db", lambda: iter([db]))

    async def _user(**kwargs):
        return user

    monkeypatch.setattr(tool_activity.User, "get", staticmethod(_user))

    result = await tool_activity.finalize_tool_run_activity(
        ToolFinalizeInput(tool_run_id=doc.id, user_id=str(user.id), reason="worker died")
    )

    assert result["settled"] is True
    settled = await ToolService.get_tool_run(user=user, db=db, tool_run_id=doc.id)
    assert settled.status == EventResultStatus.stuck
    assert settled.error_message.text == "worker died"


@pytest.mark.parametrize(
    "status",
    [
        EventResultStatus.success,
        EventResultStatus.error,
        EventResultStatus.cancelled,
    ],
)
@pytest.mark.asyncio
async def test_finaliser_never_clobbers_a_settled_run(
    status, db, user, monkeypatch, set_test_settings
):
    doc = await _make_run(user, db, status)
    monkeypatch.setattr(tool_activity, "get_mongo_db", lambda: iter([db]))

    async def _user(**kwargs):
        return user

    monkeypatch.setattr(tool_activity.User, "get", staticmethod(_user))

    result = await tool_activity.finalize_tool_run_activity(
        ToolFinalizeInput(tool_run_id=doc.id, user_id=str(user.id), reason="late")
    )

    assert result["settled"] is False
    assert (await ToolService.get_tool_run(user=user, db=db, tool_run_id=doc.id)).status == status


@pytest.mark.asyncio
async def test_finaliser_tolerates_a_deleted_run(db, user, monkeypatch, set_test_settings):
    monkeypatch.setattr(tool_activity, "get_mongo_db", lambda: iter([db]))

    async def _user(**kwargs):
        return user

    monkeypatch.setattr(tool_activity.User, "get", staticmethod(_user))

    result = await tool_activity.finalize_tool_run_activity(
        ToolFinalizeInput(
            tool_run_id="6710000000000000000000aa", user_id=str(user.id), reason="gone"
        )
    )
    assert result["settled"] is False
