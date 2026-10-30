"""Deleting is for finished runs; a live one must be cancelled first."""

from __future__ import annotations

import uuid

import pytest

from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.llm.tools.tool_result.tool_result_doc_model import ToolResultCreate, ToolRun
from src.autobots.llm.tools.tool_result.user_tool_result import UserToolResult
from src.autobots.llm.tools.tool_service import ToolRunInFlight, ToolRunNotFound, ToolService


async def _make_run(user, db, status: EventResultStatus):
    return await UserToolResult(user, db).create_tool_result(
        ToolResultCreate(
            name="get_date_time",
            status=status,
            result=ToolRun(tool_name=f"probe_{uuid.uuid4().hex[:8]}", tool_args={}),
        )
    )


@pytest.mark.parametrize(
    "status",
    [EventResultStatus.success, EventResultStatus.error, EventResultStatus.cancelled],
)
@pytest.mark.asyncio
async def test_terminal_runs_can_be_deleted(status, db, user, set_test_settings):
    doc = await _make_run(user, db, status)

    deleted = await ToolService.delete_tool_run(user=user, db=db, tool_run_id=doc.id)

    assert deleted.id == doc.id
    with pytest.raises(ToolRunNotFound):
        await ToolService.get_tool_run(user=user, db=db, tool_run_id=doc.id)


@pytest.mark.parametrize(
    "status", [EventResultStatus.processing, EventResultStatus.request_cancel]
)
@pytest.mark.asyncio
async def test_in_flight_runs_are_refused(status, db, user, set_test_settings):
    doc = await _make_run(user, db, status)

    with pytest.raises(ToolRunInFlight):
        await ToolService.delete_tool_run(user=user, db=db, tool_run_id=doc.id)

    still_there = await ToolService.get_tool_run(user=user, db=db, tool_run_id=doc.id)
    assert still_there.status == status


@pytest.mark.asyncio
async def test_unknown_run_cannot_be_deleted(db, user, set_test_settings):
    with pytest.raises(ToolRunNotFound):
        await ToolService.delete_tool_run(
            user=user, db=db, tool_run_id="6710000000000000000000aa"
        )


@pytest.mark.asyncio
async def test_delete_is_scoped_to_the_owner(db, user, set_test_settings):
    from src.autobots.user.user_orm_model import UserORM

    doc = await _make_run(user, db, EventResultStatus.success)
    other = UserORM(id="some_other_user_id", email="other@runkai.ai")

    with pytest.raises(ToolRunNotFound):
        await ToolService.delete_tool_run(user=other, db=db, tool_run_id=doc.id)

    assert (await ToolService.get_tool_run(user=user, db=db, tool_run_id=doc.id)).id == doc.id
