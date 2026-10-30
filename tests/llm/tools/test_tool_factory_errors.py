import pytest
from pydantic import BaseModel

from src.autobots.data_model.app_state import AppState
from src.autobots.llm.tools.tool_factory import ToolFactory
from src.autobots.llm.tools.tools_map import TOOLS_MAP


@pytest.mark.asyncio
async def test_failure_is_returned_as_text_by_default(ctx, set_test_settings):
    output = await ToolFactory(user=None, db=None).run_tool(
        ctx=ctx,
        app_state=AppState(),
        tool_name="no_such_tool",
        tool_args={},
    )
    assert output.text


@pytest.mark.asyncio
async def test_failure_is_raised_when_requested(ctx, set_test_settings):
    with pytest.raises(AttributeError):
        await ToolFactory(user=None, db=None).run_tool(
            ctx=ctx,
            app_state=AppState(),
            tool_name="no_such_tool",
            tool_args={},
            raise_on_error=True,
        )


class _ProbeConfig(BaseModel):
    value: str = "x"


async def _tool_returning_exception(config: _ProbeConfig, ctx) -> Exception:
    """Reports failure by returning an Exception, as many tools in this repo do."""
    return Exception("boom")


@pytest.mark.asyncio
async def test_returned_exception_is_raised_when_requested(ctx, monkeypatch, set_test_settings):
    monkeypatch.setitem(TOOLS_MAP, "probe_returns_exception", _tool_returning_exception)

    with pytest.raises(Exception, match="boom"):
        await ToolFactory(user=None, db=None).run_tool(
            ctx=ctx,
            app_state=AppState(),
            tool_name="probe_returns_exception",
            tool_args={"value": "x"},
            raise_on_error=True,
        )


@pytest.mark.asyncio
async def test_returned_exception_stays_text_for_the_llm_path(ctx, monkeypatch, set_test_settings):
    monkeypatch.setitem(TOOLS_MAP, "probe_returns_exception", _tool_returning_exception)

    output = await ToolFactory(user=None, db=None).run_tool(
        ctx=ctx,
        app_state=AppState(),
        tool_name="probe_returns_exception",
        tool_args={"value": "x"},
    )
    assert output.text == "boom"
