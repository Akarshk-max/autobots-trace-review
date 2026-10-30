import json
from typing import Any

import pytest
from pydantic import BaseModel

from src.autobots.action.action.action_doc_model import ActionFind
from src.autobots.action.action.common_action_models import TextObj
from src.autobots.data_model.app_state import AppState
from src.autobots.llm.tools.tool_factory import ToolFactory


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    ["get_tools"],  # , "find_action", ]
)
async def test_tool_factory_happy_path_1(ctx, db, user, tool_name, set_test_settings):
    tool_factory = ToolFactory(user=user, db=db)
    tool_args = await get_tool_args(tool_name)
    app_state = AppState()
    output = await tool_factory.run_tool(ctx=ctx, app_state=app_state, tool_name=tool_name, tool_args=tool_args)
    assert output


async def get_tool_args(tool_name: str) -> Any | None:
    match tool_name:
        case "get_tools":
            return TextObj(text="")
        case "find_action":
            return ActionFind()
        case _:
            return None


@pytest.mark.asyncio
async def test_tool_factory_convert_to_str_happy_path_2(set_test_settings):
    output = TextObj(text="test")
    match output:
        case BaseModel():
            output_str = output.model_dump_json(exclude_none=True)
        case str():
            output_str = output
        case list():
            output_str = json.dumps(output)
        case dict():
            output_str = json.dumps(output)
        case Exception():
            output_str = str(output)
        case _:
            output_str = output.model_dump_json(
                exclude_none=True
            )  # json.dumps(output.model_dump())

    assert output_str
