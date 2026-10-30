import pytest
from openai.types.chat import ChatCompletionUserMessageParam

from src.autobots.action.action.action_doc_model import ActionDoc, ActionResult
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    LlmChatOpenaiConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.tool_factory import ToolFactory
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.asyncio
@pytest.mark.skip("Expense Related")
async def test_action_llm_chat_openai_action_run_happy_path(
    set_test_settings, register_tools_fixture
):
    ctx = Context()
    tools = ["vector_store_handler"]
    # await ToolFactory.get_tools()
    assert tools is not None
    tool_defs = await ToolFactory.get_chat_completion_tool_param(ctx=ctx, tool_names=tools)
    assert tool_defs is not None

    action_config = LlmChatOpenaiConfig(
        provider="azure",
        messages=[ChatCompletionUserMessageParam(role="user", content="Be a helpful assistant")],
        model="gpt-4o",
        tools=tools,
    )
    action_doc = ActionDoc(
        id="action_doc_1",
        user_id="user_id_1",
        name="action_1",
        type=ActionType.text2text_llm_chat_openai,
        config=action_config.model_dump(exclude_none=True),
    )
    user = UserORM(id=str(gen_uuid()))
    db = next(get_mongo_db())
    app_state = AppState()
    action_input = DataBlock(
        text="Use vector_store_handler tool, datastore_id: test_1, put_resource the file https://app-myautobots-public-dev.s3.amazonaws.com/files/e29f3392-6124-40f5-8e24-fd6b5ebff875/Khushal_CV_cs_11.pdf"
    )
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

    action_input = {
        "text": "Use vector_store_handler, datastore_id: test_1, search for input: khushal"
    }
    action_run_obj_2 = await ActionFactory.run_action(
        Context(), app_state, action_doc, action_input, user, db=db
    )
    print(action_run_obj_2.output_dict)
    assert len(action_run_obj_2.config_dict.get("messages")) > 3
    assert action_run_obj_2.config_dict.get("messages")[1].get(
        "content"
    ) == action_run_obj_1.input_dict.get("text")
    assert action_run_obj_1.output_dict.get("blocks")[0].get(
        "text"
    ) in action_run_obj_2.config_dict.get("messages")[-3].get("content")
