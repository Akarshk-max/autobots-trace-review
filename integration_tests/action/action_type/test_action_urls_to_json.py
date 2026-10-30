import json

import pytest

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_map import ACTION_MAP
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.data_model.app_state import AppState


@pytest.mark.asyncio
async def test_action_urls_to_json_happy_path(ctx, db, user, set_test_settings):
    action_type = ActionType.urls_to_json
    action = ACTION_MAP.get(action_type)

    input_urls = [
        "https://example.com/alpha",
        "https://example.com/beta?x=1",
        "https://app-myautobots-public-dev.s3.amazonaws.com/files/demo/sample.csv",
    ]
    action_input = DataBlock(urls=input_urls)
    action_input_dict = action_input.model_dump(exclude_none=True)
    action.get_input_type().model_validate(action_input_dict)

    action_doc = ActionDoc(
        id="id_test_action_urls_to_json",
        user_id=user.id,
        name="name_test_action_urls_to_json",
        type=action_type,
        config={},
    )

    app_state = AppState()
    run_action_obj = await ActionFactory.run_action(ctx, app_state, action_doc, action_input_dict, user, db=db)
    run_output = action.get_output_type().model_validate(run_action_obj.output_dict)

    assert run_output
    assert len(run_output.blocks) == 1
    assert len(run_output.blocks[0].urls) == 1

    json_url = run_output.blocks[0].urls[0]
    s3 = get_public_s3()
    key = await s3.get_key_from_https_url(json_url)
    json_bytes = await s3.get_bytes(ctx=ctx, key=key)
    data = json.loads(json_bytes.decode("utf-8"))

    assert data["urls"] == input_urls
