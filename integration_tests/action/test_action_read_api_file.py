import pytest

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_map import ACTION_MAP
from src.autobots.action.action_type.action_text2text.action_text2text_read_api_file import (
    ActionConfig,
    ActionText2textReadApiFile,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.app_state import AppState


@pytest.mark.asyncio
async def test_action_text2text_api_1_happy_path(ctx, db, user, set_test_settings):
    action_config = ActionConfig(text="Github")
    action = ActionText2textReadApiFile(action_config=action_config, db=db, user=user)
    action_input_dict = {"text": "Github"}
    action_type = ActionType.text2text_read_api_file
    action = ACTION_MAP.get(action_type)
    action.get_input_type().model_validate(action_input_dict)
    action_doc = ActionDoc(
        id="id_test_action_text2text_api_1_happy_path",
        user_id=user.id,
        name="name_test_action_text2text_api_1_happy_path",
        type=action_type,
        config=action_input_dict,
    )
    app_state = AppState()
    run_action_obj = await ActionFactory.run_action(ctx, app_state, action_doc, action_input_dict, user, db=db)
    run_output = action.get_output_type().model_validate(run_action_obj.output_dict)
    assert run_output
