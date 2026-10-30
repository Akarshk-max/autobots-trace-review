import pytest

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_text2text.action_text2text_user_input import (
    ActionText2textUserInput,
)
from src.autobots.data_model.app_state import AppState


@pytest.mark.asyncio
async def test_action_text2text_user_input_happy_path(ctx, db, user, set_test_settings):
    action_config_create = DataBlock(text="action_config")
    action_config = await ActionText2textUserInput.create_config(
        ctx, action_config_create, user=user, db=db
    )
    action = ActionText2textUserInput(action_config=action_config, db=db, user=user)

    action_input = DataBlock(text="action_input")
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    assert len(action_output.blocks) == 2
    assert action_output.blocks[0].text == "action_config"
    assert action_output.blocks[1].text == "action_input"


@pytest.mark.asyncio
async def test_action_text2text_user_input_empty_config_happy_path(
    ctx, db, user, set_test_settings
):
    action_config_create = DataBlock()
    action_config = await ActionText2textUserInput.create_config(
        ctx, action_config_create, user=user, db=db
    )
    action = ActionText2textUserInput(action_config=action_config, db=db, user=user)

    action_input = DataBlock(text="action_input")
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    assert len(action_output.blocks) == 1
