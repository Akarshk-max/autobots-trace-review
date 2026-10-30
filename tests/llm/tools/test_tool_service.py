"""Schemas the tool API publishes must carry real types, and unknown tools must say so."""
import pytest
from pydantic import BaseModel

from src.autobots.llm.tools.tool_service import (
    ToolConfigUnavailable,
    ToolNotFound,
    ToolService,
)
from src.autobots.llm.tools.tools_map import TOOLS_MAP


def _type_erased(input_schema: dict) -> list[str]:
    erased = []
    for name, prop in (input_schema.get("properties") or {}).items():
        any_of = prop.get("anyOf")
        if isinstance(any_of, list) and any_of and all(o.get("type") == "null" for o in any_of):
            erased.append(name)
    return erased


def test_list_tool_names_is_sorted_and_populated(set_test_settings):
    names = ToolService.list_tool_names()
    assert names == sorted(names)
    assert len(names) > 100
    assert "sql_runner" in names


def test_unknown_tool_raises_not_found(set_test_settings):
    with pytest.raises(ToolNotFound):
        ToolService.get_tool_config("no_such_tool")


def test_description_is_returned_for_a_known_tool(set_test_settings):
    described = ToolService.get_tool_description("sql_runner")
    assert described.name == "sql_runner"
    assert "connection" in described.description.lower()


def test_description_works_for_tools_without_a_schema(set_test_settings):
    """Description does not depend on a resolvable config model, unlike get_tool_config."""
    with pytest.raises(ToolConfigUnavailable):
        ToolService.get_tool_config("claude_container_file_attach")

    described = ToolService.get_tool_description("claude_container_file_attach")
    assert described.description


def test_unknown_tool_has_no_description(set_test_settings):
    with pytest.raises(ToolNotFound):
        ToolService.get_tool_description("no_such_tool")


def test_every_tool_has_a_description(set_test_settings):
    missing = [
        name
        for name in ToolService.list_tool_names()
        if not ToolService.get_tool_description(name).description
    ]
    assert missing == []


def test_config_carries_description_and_schema(set_test_settings):
    config = ToolService.get_tool_config("sql_runner")
    assert config.name == "sql_runner"
    assert config.description
    assert config.input_schema["type"] == "object"
    assert "connection_name" in config.input_schema["properties"]


def test_optional_fields_keep_their_type(set_test_settings):
    """The OpenAI definition generator renders Optional[X] as null|null; this must not."""
    config = ToolService.get_tool_config("sql_runner")
    sql_query = config.input_schema["properties"]["sql_query"]
    assert {"type": "string"} in sql_query["anyOf"]
    assert _type_erased(config.input_schema) == []


@pytest.mark.parametrize("tool_name", ["perplexity_search", "api_call", "llm_chat"])
def test_no_type_erasure_across_heavy_config_tools(tool_name, set_test_settings):
    config = ToolService.get_tool_config(tool_name)
    assert _type_erased(config.input_schema) == []


def test_every_tool_either_resolves_a_schema_or_reports_cleanly(set_test_settings):
    unavailable = []
    for name in ToolService.list_tool_names():
        try:
            config = ToolService.get_tool_config(name)
        except ToolConfigUnavailable:
            unavailable.append(name)
            continue
        assert config.input_schema.get("type") == "object", name

    assert unavailable == ["claude_container_file_attach"]


def test_config_model_resolves_both_registry_shapes(set_test_settings):
    import inspect

    function_tool = TOOLS_MAP["get_date_time"]
    action_tool = TOOLS_MAP["sql_runner"]

    assert inspect.isfunction(function_tool)
    assert not inspect.isfunction(action_tool)

    for tool in (function_tool, action_tool):
        model = ToolService._config_model(tool)
        assert model is not None and issubclass(model, BaseModel)
