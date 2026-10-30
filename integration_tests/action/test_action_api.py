import pytest

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_map import ACTION_MAP
from src.autobots.action.action_type.action_text2text.action_api import ActionAPI, APIRequest
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.app_state import AppState


@pytest.mark.asyncio
async def test_action_api_1_happy_path(ctx, db, user, set_test_settings):
    """
    Direct use of ActionAPI.run_action with a public endpoint.
    """
    action_config = APIRequest(
        method="GET",
        url="https://api.adviceslip.com/advice",
        headers={"Content-Type": "application/json"},
    )
    action = ActionAPI(action_config, user=user, db=db)

    # ActionAPI expects a DataBlock whose text is a JSON string with overrides.
    # No overrides needed for this simple GET.
    action_input = DataBlock(text="{}")

    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)
    assert isinstance(action_output, DataBlocks)
    assert len(action_output.blocks) > 0
    assert "slip" in action_output.blocks[0].text


@pytest.mark.asyncio
async def test_action_api_2_happy_path(ctx, db, user, set_test_settings):
    """
    Execute ActionAPI via ActionFactory to ensure factory integration works.
    """
    action_type = ActionType.api
    action_cls = ACTION_MAP.get(action_type)

    config_dict = {
        "method": "GET",
        "url": "https://api.adviceslip.com/advice",
        "headers": {"Content-Type": "application/json"},
        "params": {  # <-- query parameters for the API call
            "username": "niel-mohan",
        },
    }
    # Validate config
    action_cls.get_config_type().model_validate(config_dict)

    action_doc = ActionDoc(
        id="id_test_action_api_factory",
        user_id=user.id,
        name="name_test_action_api_factory",
        type=action_type,
        config=config_dict,
    )

    # Input for ActionAPI is DataBlock; supply empty overrides.
    input_dict = {"text": "{}"}

    app_state = AppState()
    run_action_obj = await ActionFactory.run_action(ctx, app_state, action_doc, input_dict, user, db=db)
    run_output = action_cls.get_output_type().model_validate(run_action_obj.output_dict)
    assert isinstance(run_output, DataBlocks)
    assert len(run_output.blocks) > 0
    assert "slip" in run_output.blocks[0].text


@pytest.mark.asyncio
async def test_action_api_3_happy_path(ctx, db, user, set_test_settings):
    """
    Execute ActionAPI via ActionFactory to ensure factory integration works.
    """
    action_type = ActionType.api
    action_cls = ACTION_MAP.get(action_type)

    config_dict = {
        "method": "GET",
        "url": "https://linkedin-scraper-api-real-time-fast-affordable.p.rapidapi.com/profile/detail",
        "headers": {
            "x-rapidapi-host": "linkedin-scraper-api-real-time-fast-affordable.p.rapidapi.com",
            "x-rapidapi-key": "ecea9bdc8dmsh3c0ca4db5c487d1p136582jsn13b3cb539df0",
        },
    }
    # Validate config
    action_cls.get_config_type().model_validate(config_dict)

    action_doc = ActionDoc(
        id="id_test_action_api_factory",
        user_id=user.id,
        name="name_test_action_api_factory",
        type=action_type,
        config=config_dict,
    )

    # Input for ActionAPI is DataBlock; supply empty overrides.
    input_dict = {"text": '{"params": {"username": "niel-mohan"}}'}

    app_state = AppState()
    run_action_obj = await ActionFactory.run_action(ctx, app_state, action_doc, input_dict, user, db=db)
    run_output = action_cls.get_output_type().model_validate(run_action_obj.output_dict)
    assert run_output.blocks[0].text
