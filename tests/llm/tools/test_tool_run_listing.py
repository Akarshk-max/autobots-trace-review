import uuid
import pytest
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.llm.tools.tool_result.tool_result_doc_model import (
    ToolResultCreate,
    ToolResultFind,
    ToolRun,
    ToolRunFind,
)
from src.autobots.llm.tools.tool_result.user_tool_result import UserToolResult
from src.autobots.llm.tools.tool_service import ToolService


async def _make_run(user, db, tool_name: str, status: EventResultStatus, output=None):
    return await UserToolResult(user, db).create_tool_result(
        ToolResultCreate(
            name=tool_name,
            status=status,
            result=ToolRun(tool_name=tool_name, tool_args={"a": 1}, output=output),
        )
    )


@pytest.fixture
def marker() -> str:
    return f"probe_{uuid.uuid4().hex[:10]}"


@pytest.mark.asyncio
async def test_filter_by_tool_name(db, user, marker, set_test_settings):
    await _make_run(user, db, marker, EventResultStatus.success)
    await _make_run(user, db, f"{marker}_other", EventResultStatus.success)

    found = await ToolService.list_tool_runs(
        user=user, db=db, tool_result_find=ToolResultFind(result=ToolRunFind(tool_name=marker))
    )

    assert found.total_count == 1
    assert found.docs[0].result.tool_name == marker


@pytest.mark.asyncio
async def test_filter_by_status(db, user, marker, set_test_settings):
    await _make_run(user, db, marker, EventResultStatus.success)
    await _make_run(user, db, marker, EventResultStatus.error)

    succeeded = await ToolService.list_tool_runs(
        user=user,
        db=db,
        tool_result_find=ToolResultFind(
            result=ToolRunFind(tool_name=marker), status=EventResultStatus.success
        ),
    )

    assert succeeded.total_count == 1
    assert succeeded.docs[0].status == EventResultStatus.success


@pytest.mark.asyncio
async def test_listing_omits_the_output_payload(db, user, marker, set_test_settings):
    await _make_run(
        user, db, marker, EventResultStatus.success, output={"text": "x" * 5000, "urls": []}
    )

    found = await ToolService.list_tool_runs(
        user=user, db=db, tool_result_find=ToolResultFind(result=ToolRunFind(tool_name=marker))
    )

    lite = found.docs[0].result
    assert lite.tool_name == marker
    assert lite.tool_args == {"a": 1}
    assert not hasattr(lite, "output")


@pytest.mark.asyncio
async def test_offset_is_a_page_index(db, user, marker, set_test_settings):
    for _ in range(3):
        await _make_run(user, db, marker, EventResultStatus.success)

    find = ToolResultFind(result=ToolRunFind(tool_name=marker))
    first = await ToolService.list_tool_runs(user=user, db=db, tool_result_find=find, limit=2, offset=0)
    second = await ToolService.list_tool_runs(user=user, db=db, tool_result_find=find, limit=2, offset=1)

    assert first.total_count == 3
    assert len(first.docs) == 2
    assert len(second.docs) == 1
    assert {d.id for d in first.docs}.isdisjoint({d.id for d in second.docs})


@pytest.mark.asyncio
async def test_runs_are_scoped_to_the_owner(db, user, marker, set_test_settings):
    from src.autobots.user.user_orm_model import UserORM

    await _make_run(user, db, marker, EventResultStatus.success)
    other = UserORM(id="some_other_user_id", email="other@runkai.ai")

    found = await ToolService.list_tool_runs(
        user=other, db=db, tool_result_find=ToolResultFind(result=ToolRunFind(tool_name=marker))
    )

    assert found.total_count == 0
