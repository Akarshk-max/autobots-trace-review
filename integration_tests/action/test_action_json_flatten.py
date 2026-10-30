import json

import pytest

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_map import ACTION_MAP
from src.autobots.action.action_type.action_text2text.action_json_flatten import (
    ActionJsonFlatten,
    JsonFlattenConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.app_state import AppState


@pytest.mark.asyncio
async def test_action_json_flatten_simple_nested(ctx, db, user, set_test_settings):
    """Test flattening a simple nested JSON object."""
    action_config = JsonFlattenConfig()
    action = ActionJsonFlatten(action_config, user=user, db=db)
    action_input = DataBlock(text='{"a": {"b": 5}}')
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    if isinstance(action_output, Exception):
        pytest.fail(f"Action returned exception: {action_output}")

    assert isinstance(action_output, DataBlocks)
    assert len(action_output.blocks) == 1
    expected = json.loads('{"a|b": 5}')
    actual = json.loads(action_output.blocks[0].text)
    assert actual == expected


@pytest.mark.asyncio
async def test_action_json_flatten_multiple_keys(ctx, db, user, set_test_settings):
    """Test flattening JSON with multiple nested keys."""
    action_config = JsonFlattenConfig()
    action = ActionJsonFlatten(action_config, user=user, db=db)
    action_input = DataBlock(text='{"a": {"b": 5, "c": 10}, "d": {"e": 20}}')
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    if isinstance(action_output, Exception):
        pytest.fail(f"Action returned exception: {action_output}")

    expected = json.loads('{"a|b": 5, "a|c": 10, "d|e": 20}')
    actual = json.loads(action_output.blocks[0].text)
    assert actual == expected


@pytest.mark.asyncio
async def test_action_json_flatten_deep_nesting(ctx, db, user, set_test_settings):
    """Test flattening deeply nested JSON."""
    action_config = JsonFlattenConfig()
    action = ActionJsonFlatten(action_config, user=user, db=db)
    action_input = DataBlock(text='{"a": {"b": {"c": {"d": "deep"}}}}')
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    if isinstance(action_output, Exception):
        pytest.fail(f"Action returned exception: {action_output}")

    expected = json.loads('{"a|b|c|d": "deep"}')
    actual = json.loads(action_output.blocks[0].text)
    assert actual == expected


@pytest.mark.asyncio
async def test_action_json_flatten_with_array(ctx, db, user, set_test_settings):
    """Test that arrays are preserved and not flattened."""
    action_config = JsonFlattenConfig()
    action = ActionJsonFlatten(action_config, user=user, db=db)
    action_input = DataBlock(
        text='{"user": {"name": "John", "age": 30, "active": true, "email": null, "tags": ["admin", "user"]}}'
    )
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    if isinstance(action_output, Exception):
        pytest.fail(f"Action returned exception: {action_output}")

    expected = json.loads(
        '{"user|name": "John", "user|age": 30, "user|active": true, "user|email": null, "user|tags": ["admin", "user"]}'
    )
    actual = json.loads(action_output.blocks[0].text)
    assert actual == expected


@pytest.mark.asyncio
async def test_action_json_flatten_complex(ctx, db, user, set_test_settings):
    """Test flattening complex JSON with mixed nested objects and arrays."""
    action_config = JsonFlattenConfig()
    action = ActionJsonFlatten(action_config, user=user, db=db)
    action_input = DataBlock(
        text='{"company": {"name": "TechCorp", "employees": [{"id": 1}, {"id": 2}], "address": {"city": "NYC", "details": {"zip": "10001", "street": "5th Ave"}}}}'
    )
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    if isinstance(action_output, Exception):
        pytest.fail(f"Action returned exception: {action_output}")

    expected = json.loads(
        '{"company|name": "TechCorp", "company|employees": [{"id": 1}, {"id": 2}], "company|address|city": "NYC", "company|address|details|zip": "10001", "company|address|details|street": "5th Ave"}'
    )
    actual = json.loads(action_output.blocks[0].text)
    assert actual == expected


@pytest.mark.asyncio
async def test_action_json_flatten_via_action_factory(ctx, db, user, set_test_settings):
    """Test running the action via ActionFactory."""
    action_type = ActionType.json_flatten
    action = ACTION_MAP.get(action_type)

    action_input_dict = {"text": '{"a": {"b": 5}}'}
    action.get_input_type().model_validate(action_input_dict)

    action_doc = ActionDoc(
        id="id_test_action_json_flatten",
        user_id=user.id,
        name="name_test_action_json_flatten",
        type=action_type,
        config={},
    )
    app_state = AppState()
    run_action_obj = await ActionFactory.run_action(ctx, app_state, action_doc, action_input_dict, user, db=db)
    run_output = action.get_output_type().model_validate(run_action_obj.output_dict)

    expected = json.loads('{"a|b": 5}')
    actual = json.loads(run_output.blocks[0].text)
    assert actual == expected


@pytest.mark.asyncio
async def test_action_json_flatten_invalid_json(ctx, db, user, set_test_settings):
    """Test that invalid JSON returns an exception."""
    action_config = JsonFlattenConfig()
    action = ActionJsonFlatten(action_config, user=user, db=db)
    action_input = DataBlock(text='{"invalid json}')
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    assert isinstance(action_output, Exception)


@pytest.mark.asyncio
async def test_action_json_flatten_empty_text(ctx, db, user, set_test_settings):
    """Test that empty text returns an exception."""
    action_config = JsonFlattenConfig()
    action = ActionJsonFlatten(action_config, user=user, db=db)
    action_input = DataBlock(text="")
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    assert isinstance(action_output, Exception)


@pytest.mark.asyncio
async def test_action_json_flatten_csv_output(ctx, db, user, set_test_settings):
    """Test flattening JSON with CSV output type."""
    action_config = JsonFlattenConfig(output_type="csv")
    action = ActionJsonFlatten(action_config, user=user, db=db)
    action_input = DataBlock(text='{"a": {"b": 5, "c": 10}}')
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    if isinstance(action_output, Exception):
        pytest.fail(f"Action returned exception: {action_output}")

    assert isinstance(action_output, DataBlocks)
    assert len(action_output.blocks) == 1

    # CSV should have headers and values
    csv_output = action_output.blocks[0].text
    lines = csv_output.strip().split("\n")
    assert len(lines) == 2  # Header + 1 data row
    assert "a|b" in lines[0]
    assert "a|c" in lines[0]


@pytest.mark.asyncio
async def test_action_json_flatten_csv_with_array(ctx, db, user, set_test_settings):
    """Test CSV output with array values (should be serialized as JSON string)."""
    action_config = JsonFlattenConfig(output_type="csv")
    action = ActionJsonFlatten(action_config, user=user, db=db)
    action_input = DataBlock(text='{"user": {"name": "John", "tags": ["admin", "user"]}}')
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    if isinstance(action_output, Exception):
        pytest.fail(f"Action returned exception: {action_output}")

    csv_output = action_output.blocks[0].text
    lines = csv_output.strip().split("\n")
    assert len(lines) == 2
    # Array should be serialized as JSON string in CSV
    assert "user|name" in lines[0]
    assert "user|tags" in lines[0]
    assert "John" in lines[1]


@pytest.mark.asyncio
async def test_action_json_flatten_dict_to_csv_static_method(ctx, db, user, set_test_settings):
    """Test the dict_to_csv static method directly."""
    flattened_dict = {"a|b": 5, "a|c": "hello", "d|e": [1, 2, 3]}
    csv_output = ActionJsonFlatten.dict_to_csv(flattened_dict)

    lines = csv_output.strip().split("\n")
    assert len(lines) == 2

    # Check headers - strip any carriage returns for cross-platform compatibility
    headers = [h.strip() for h in lines[0].split(",")]
    assert "a|b" in headers
    assert "a|c" in headers
    assert "d|e" in headers

    # Check that array is serialized as JSON
    assert "[1, 2, 3]" in lines[1] or "[1,2,3]" in lines[1]
