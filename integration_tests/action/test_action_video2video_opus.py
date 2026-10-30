import pytest

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action_type.action_factory import ActionFactory, RunActionObj
from src.autobots.action.action_type.action_video2video.action_video2video_opus import (
    ActionVideo2VideoOpus,
    Video2VideoRunModel,
)
from src.autobots.conn.opus.opus import Video2VideoReqModel
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.exception.app_exception import AppException
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.asyncio
@pytest.mark.skip(reason="Expensive to test everytime")
async def test_action_video2video_opus_happy_path(set_test_settings):
    action_config = Video2VideoReqModel()
    action_input = Video2VideoRunModel(url="https://www.youtube.com/watch?v=hS5CfP8n_js")
    action_doc = ActionDoc(
        id="1",
        user_id="user_1",
        name="Action_for_celery",
        type=ActionVideo2VideoOpus.type,
        config=action_config.model_dump(exclude_none=True),
    )
    user = UserORM(id=str(gen_uuid()))
    db = next(get_mongo_db())
    run_action_object: RunActionObj = await ActionFactory.run_action(
        Context(), AppState(), action_doc, action_input.model_dump(exclude_none=True), user, db=db
    )
    if isinstance(run_action_object, AppException):
        assert run_action_object.detail is None
    action_output = ActionVideo2VideoOpus.get_output_type().model_validate(
        run_action_object.output_dict
    )
    assert action_output.url is not None
