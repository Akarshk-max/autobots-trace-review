import uuid
import pytest
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.llm.tools import tool_service
from src.autobots.llm.tools.tool_result.tool_result_doc_model import (
    ToolResultCreate,
    ToolResultUpdate,
    ToolRun,
)
from src.autobots.llm.tools.tool_result.user_tool_result import UserToolResult
from src.autobots.llm.tools.tool_service import ToolRunNotFound, ToolService


async def _make_run(user, db, status: EventResultStatus):
    return await UserToolResult(user, db).create_tool_result(
        ToolResultCreate(
            name="get_date_time",
            status=status,
            result=ToolRun(tool_name=f"probe_{uuid.uuid4().hex[:8]}", tool_args={}),
        )
    )


def _cancel_workflow(returns: bool, captured: dict):
    async def cancel(workflow_id: str) -> bool:
        captured["workflow_id"] = workflow_id
        return returns

    return cancel


@pytest.mark.asyncio
async def test_running_run_moves_to_request_cancel(db, user, monkeypatch, set_test_settings):
    captured: dict = {}
    monkeypatch.setattr(tool_service, "cancel_workflow", _cancel_workflow(True, captured))
    doc = await _make_run(user, db, EventResultStatus.processing)

    cancelled = await ToolService.cancel_tool_run(user=user, db=db, tool_run_id=doc.id)

    assert cancelled.status == EventResultStatus.request_cancel
    assert captured["workflow_id"] == f"tool-{doc.id}"


@pytest.mark.asyncio
async def test_run_with_no_live_workflow_is_finalised(db, user, monkeypatch, set_test_settings):
    monkeypatch.setattr(tool_service, "cancel_workflow", _cancel_workflow(False, {}))
    doc = await _make_run(user, db, EventResultStatus.processing)

    cancelled = await ToolService.cancel_tool_run(user=user, db=db, tool_run_id=doc.id)

    assert cancelled.status == EventResultStatus.cancelled


@pytest.mark.parametrize(
    "status",
    [EventResultStatus.success, EventResultStatus.error, EventResultStatus.cancelled],
)
@pytest.mark.asyncio
async def test_terminal_runs_are_left_alone(status, db, user, monkeypatch, set_test_settings):
    captured: dict = {}
    monkeypatch.setattr(tool_service, "cancel_workflow", _cancel_workflow(True, captured))
    doc = await _make_run(user, db, status)

    unchanged = await ToolService.cancel_tool_run(user=user, db=db, tool_run_id=doc.id)

    assert unchanged.status == status
    assert captured == {}


@pytest.mark.asyncio
async def test_cancelling_twice_is_a_no_op(db, user, monkeypatch, set_test_settings):
    captured: dict = {}
    monkeypatch.setattr(tool_service, "cancel_workflow", _cancel_workflow(True, captured))
    doc = await _make_run(user, db, EventResultStatus.request_cancel)

    again = await ToolService.cancel_tool_run(user=user, db=db, tool_run_id=doc.id)

    assert again.status == EventResultStatus.request_cancel
    assert captured == {}


@pytest.mark.asyncio
async def test_unknown_run_cannot_be_cancelled(db, user, set_test_settings):
    with pytest.raises(ToolRunNotFound):
        await ToolService.cancel_tool_run(
            user=user, db=db, tool_run_id="6710000000000000000000aa"
        )


@pytest.mark.asyncio
async def test_cancelled_run_keeps_no_partial_output(db, user, monkeypatch, set_test_settings):
    """Tools have no intermediate state: a cancelled run is cancelled whole, with no output."""
    monkeypatch.setattr(tool_service, "cancel_workflow", _cancel_workflow(True, {}))
    doc = await _make_run(user, db, EventResultStatus.processing)
    tool_name = doc.result.tool_name

    await ToolService.cancel_tool_run(user=user, db=db, tool_run_id=doc.id)

    # What the runner writes when the activity is cancelled.
    finalised = await UserToolResult(user, db).update_tool_result(
        doc.id, ToolResultUpdate(status=EventResultStatus.cancelled)
    )

    assert finalised.status == EventResultStatus.cancelled
    assert finalised.result.output is None
    assert finalised.result.tool_name == tool_name
    assert finalised.result.tool_args == {}
