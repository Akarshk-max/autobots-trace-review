"""Two writers race to settle a run: the activity and the API. Only one may win."""
from __future__ import annotations
import uuid
import pytest

from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.llm.tools import tool_service
from src.autobots.llm.tools.tool_result.tool_result_doc_model import (
    TERMINAL_TOOL_RUN_STATUSES,
    ToolResultCreate,
    ToolResultUpdate,
    ToolRun,
)
from src.autobots.llm.tools.tool_result.user_tool_result import UserToolResult
from src.autobots.llm.tools.tool_service import ToolArgsInvalid, ToolService


async def _make_run(user, db, status: EventResultStatus, output=None):
    return await UserToolResult(user, db).create_tool_result(
        ToolResultCreate(
            name="get_date_time",
            status=status,
            result=ToolRun(
                tool_name=f"probe_{uuid.uuid4().hex[:8]}", tool_args={}, output=output
            ),
        )
    )


def test_stuck_counts_as_terminal():
    assert EventResultStatus.stuck in TERMINAL_TOOL_RUN_STATUSES


@pytest.mark.asyncio
async def test_a_stuck_run_can_be_deleted(db, user, set_test_settings):
    doc = await _make_run(user, db, EventResultStatus.stuck)

    deleted = await ToolService.delete_tool_run(user=user, db=db, tool_run_id=doc.id)

    assert deleted.id == doc.id


@pytest.mark.asyncio
async def test_cancelling_a_stuck_run_leaves_it_alone(db, user, monkeypatch, set_test_settings):
    called = {}

    async def _cancel(workflow_id):
        called["hit"] = True
        return True

    monkeypatch.setattr(tool_service, "cancel_workflow", _cancel)
    doc = await _make_run(user, db, EventResultStatus.stuck)

    unchanged = await ToolService.cancel_tool_run(user=user, db=db, tool_run_id=doc.id)

    assert unchanged.status == EventResultStatus.stuck
    assert called == {}


#  compare-and-set


@pytest.mark.asyncio
async def test_settle_writes_when_the_run_is_still_open(db, user, set_test_settings):
    doc = await _make_run(user, db, EventResultStatus.processing)

    settled = await UserToolResult(user, db).settle_tool_result(
        doc.id, ToolResultUpdate(status=EventResultStatus.error)
    )

    assert settled is not None and settled.status == EventResultStatus.error


@pytest.mark.parametrize(
    "status",
    [
        EventResultStatus.success,
        EventResultStatus.error,
        EventResultStatus.cancelled,
        EventResultStatus.stuck,
    ],
)
@pytest.mark.asyncio
async def test_settle_refuses_to_overwrite_a_terminal_run(status, db, user, set_test_settings):
    doc = await _make_run(user, db, status, output={"text": "keep me"})

    settled = await UserToolResult(user, db).settle_tool_result(
        doc.id, ToolResultUpdate(status=EventResultStatus.cancelled)
    )

    assert settled is None
    still = await ToolService.get_tool_run(user=user, db=db, tool_run_id=doc.id)
    assert still.status == status
    assert still.result.output == {"text": "keep me"}


# the actual race


@pytest.mark.asyncio
async def test_a_run_that_completes_mid_cancel_keeps_its_result(
    db, user, monkeypatch, set_test_settings
):
    """The activity finishes between the request_cancel write and the Temporal RPC."""
    doc = await _make_run(user, db, EventResultStatus.processing)

    async def _activity_wins_the_race(workflow_id):
        await UserToolResult(user, db).settle_tool_result(
            doc.id,
            ToolResultUpdate(
                status=EventResultStatus.success,
                result=ToolRun(
                    tool_name="get_date_time", tool_args={}, output={"text": "the real result"}
                ),
            ),
        )
        return False  # workflow already gone, as it would be

    monkeypatch.setattr(tool_service, "cancel_workflow", _activity_wins_the_race)

    outcome = await ToolService.cancel_tool_run(user=user, db=db, tool_run_id=doc.id)

    assert outcome.status == EventResultStatus.success
    assert outcome.result.output == {"text": "the real result"}


# arg validation


@pytest.mark.asyncio
async def test_bad_args_are_rejected_before_a_record_exists(ctx, db, user, set_test_settings):
    with pytest.raises(ToolArgsInvalid):
        await ToolService.run_tool(
            ctx=ctx, user=user, db=db, tool_name="get_date_time", tool_args={"timezone": 123}
        )


@pytest.mark.asyncio
async def test_good_args_pass_validation(set_test_settings):
    """Validation must not reject a payload the tool would have accepted."""
    ToolService._validate_tool_args("get_date_time", {"timezone": "UTC"})
    ToolService._validate_tool_args("sleep", {"sleep_sec": 60})
    # a tool with no resolvable config model is not blocked
    ToolService._validate_tool_args("claude_container_file_attach", {"anything": 1})
