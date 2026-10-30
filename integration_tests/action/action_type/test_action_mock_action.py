import pytest

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_mock.action_mock import MockAction
from src.autobots.data_model.app_state import AppState


@pytest.mark.asyncio
async def test_action_mock_action_run_happy_path(ctx, db, user, set_test_settings):
    data = DataBlock(text="ok", urls=["https://runkai.ai"])
    app_state = AppState()
    action = MockAction(action_config=DataBlock(), user=user, db=db)
    output = await action.run_action(ctx=ctx, app_state=app_state, action_input=data)
    assert isinstance(output, DataBlocks)
    first_block = output.blocks.pop()
    assert first_block.text == data.text
    assert first_block.urls == data.urls
