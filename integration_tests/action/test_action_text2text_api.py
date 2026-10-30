import pytest

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_map import ACTION_MAP
from src.autobots.action.action_type.action_text2text.action_text2text_api import (
    ActionText2textAPI,
    APIInput,
    APIRequest,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.integrations.app_types import AppTypes
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.asyncio
async def test_action_text2text_api_1_happy_path(ctx, db, user, set_test_settings):
    action_config = APIRequest(
        method="GET",
        url="https://api.adviceslip.com/advice",
        headers={"Content-Type": "application/json"},
    )
    action = ActionText2textAPI(action_config, user=user, db=db)
    action_input = APIInput()
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)
    if isinstance(action_output, Exception):
        assert action_output is None
    assert action_output.status_code == 200
    assert action_output.content.__contains__("slip")


@pytest.mark.asyncio
async def test_action_text2text_api_2_happy_path(ctx, db, user, set_test_settings):
    action_type = ActionType.text2text_api_call
    action = ACTION_MAP.get(action_type)
    action_input_dict = {
        "method": "GET",
        "url": "https://api.adviceslip.com/advice",
        "headers": {"Content-Type": "application/json"},
    }
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
    assert run_output.status_code == 200
    assert run_output.content.__contains__("slip")


@pytest.mark.skip("For now")
@pytest.mark.asyncio
async def test_action_text2text_api_slack_post_message_happy_path(set_test_settings):
    action_type = ActionType.text2text_api_call
    action = ACTION_MAP.get(action_type)
    action_input_dict = {
        "method": "POST",
        "url": "https://slack.com/api/chat.postMessage",
        "headers": {
            # "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer xoxb-abc"
        },
        "body": {"channel": "C07BRNL20CA", "text": "Hello world :tada:"},
    }
    action.get_input_type().model_validate(action_input_dict)
    action_doc = ActionDoc(
        id="id_test_action_text2text_api_slack_post_message_happy_path",
        user_id="user_test_action_text2text_api_slack_post_message_happy_path",
        name="name_test_action_text2text_api_slack_post_message_happy_path",
        type=action_type,
        config=action_input_dict,
    )
    user = UserORM(id=str(gen_uuid()))
    db = next(get_mongo_db())
    run_action_obj = await ActionFactory.run_action(Context(), AppState(), action_doc, {}, user, db=db)
    run_output = action.get_output_type().model_validate(run_action_obj.output_dict)
    assert run_output.status_code == 200
    assert run_output.text.__contains__('"ok":true')


@pytest.mark.skip("For now")
@pytest.mark.asyncio
async def test_action_text2text_api_intuit_happy_path(set_test_settings, ctx, db, user):
    action_type = ActionType.text2text_api_call
    # action = ACTION_MAP.get(action_type)

    # user_id = "9f6ab403-3d77-407e-912b-2438f09487d7"
    # user = UserORM(id=user_id)
    # db = next(get_mongo_db())
    user_actions = UserActions(user=user, db=db)
    ctx = Context()
    input = {
        "app_name": AppTypes.intuit,
        "method": "GET",
        "url": "https://slack.com/v3/company/realm_id/account/1",
    }
    action_doc = ActionDoc(
        _id="id",
        user_id=user.id,
        name="test_intuit",
        type=action_type,
        config=input,
    )
    app_state = AppState()
    action_doc = await user_actions.run_action_doc(
        ctx=ctx, app_state=app_state, action_doc=action_doc, input=input, user=user, db=db
    )
    assert action_doc.output
