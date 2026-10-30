import pytest

from src.autobots.action.action.action_doc_model import ActionDoc, ActionResult
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_multicloud_router.action_multicloud_router_model import (
    RouterConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.asyncio
@pytest.mark.skip("Expense Related")
async def test_action_llm_chat_openai_action_run_happy_path(
    set_test_settings, register_tools_fixture
):
    ctx = Context()
    assert ctx.trace_id

    action_config = RouterConfig(
        engine={"cloud": "azure", "model": "gpt-4o"}, datastore_id="test_2"
    )
    action_doc = ActionDoc(
        id="action_doc_1",
        user_id="user_id_1",
        name="action_1",
        type=ActionType.multicloud_router_search,
        config=action_config.model_dump(exclude_none=True),
    )
    user = UserORM(id=str(gen_uuid()))
    db = next(get_mongo_db())
    app_state = AppState()
    action_input = DataBlock(text="When did khushal graduate")
    # action_input = {"text": "Learn about Runkai and Research competitors of Runkai"}
    action_run_obj_1 = await ActionFactory.run_action(
        Context(), app_state, action_doc, action_input.model_dump(), user, db=db
    )
    assert action_run_obj_1.output_dict
    assert len(action_run_obj_1.config_dict.get("messages")) > 1

    action_doc.config = action_run_obj_1.config_dict
    action_doc.input = action_run_obj_1.input_dict
    action_doc.output = action_run_obj_1.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))
