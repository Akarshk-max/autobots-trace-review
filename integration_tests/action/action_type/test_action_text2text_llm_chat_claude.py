import pytest

from src.autobots.action.action.action_doc_model import ActionDoc, ActionResult
from src.autobots.action.action.common_action_models import DataBlocks
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.claude.chat_model import ChatReqClaude, ClaudeMessageParam
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.tool_factory_claude import ToolFactoryClaude
from src.autobots.user.user_orm_model import UserORM

action_inputs = [
    {"text": "Learn about Runkai.ai"},
    {
        "text": "Explain and summarize and create code for:",
        "urls": [
            "https://app-myautobots-public-dev.s3.us-west-2.amazonaws.com/files/9f6ab403-3d77-407e-912b-2438f09487d7/NIPS-2017-attention-is-all-you-need-Paper.pdf"
        ],
    },
]


@pytest.mark.asyncio
@pytest.mark.skip
@pytest.mark.parametrize("action_input", action_inputs)
async def test_action_multimodal_llm_chat_claude_action_tool_rerun_happy_path_1(
    action_input, register_tools_fixture
):
    tools = await ToolFactoryClaude.get_tools()
    assert tools is not None
    tool_defs = await ToolFactoryClaude.get_tool_definations(tools)
    assert tool_defs is not None

    action_config = ChatReqClaude(
        messages=[
            ClaudeMessageParam(role="user", content="Be a helpful assistant"),
        ],
        # model="claude-3-5-sonnet-20240620",
        tools=tools,
        thinking={"type": "enabled", "budget_tokens": 2048},
    )
    user = UserORM(id=str(gen_uuid()))
    action_doc = ActionDoc(
        id="action_doc_1",
        user_id="user_id_1",
        name="action_1",
        type=ActionType.text2text_llm_chat_claude,
        config=action_config.model_dump(exclude_none=True),
    )

    # action_input = {
    #     "text": "Explain and summarize and create code for:",
    #     "urls": ["https://app-myautobots-public-dev.s3.us-west-2.amazonaws.com/files/9f6ab403-3d77-407e-912b-2438f09487d7/NIPS-2017-attention-is-all-you-need-Paper.pdf"]
    # }
    db = next(get_mongo_db())
    app_state = AppState()
    action_run_obj_1 = await ActionFactory.run_action(
        Context(), app_state, action_doc, action_input, user, db=db
    )
    assert action_run_obj_1.output_dict
    assert not isinstance(action_run_obj_1.output_dict, Exception)
    # assert len(action_run_obj_1.config_dict.get("messages")) > 1

    action_doc.config = action_run_obj_1.config_dict
    action_doc.input = action_run_obj_1.input_dict
    action_doc.output = action_run_obj_1.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))

    action_input = {"text": "Keep the same plot but make it funny"}
    action_run_obj_2 = await ActionFactory.run_action(
        Context(), AppState(), action_doc, action_input, user, db=db
    )
    assert action_run_obj_2.output_dict
    assert not isinstance(action_run_obj_2.output_dict, Exception)
    # assert len(action_run_obj_2.config_dict.get("messages")) > 3
    # assert action_run_obj_2.config_dict.get("messages")[1].get("content") == action_run_obj_1.input_dict.get("text")
    # assert action_run_obj_1.output_dict.get("texts")[0].get("text") in action_run_obj_2.config_dict.get("messages")[-2].get("content")


@pytest.mark.asyncio
@pytest.mark.skip
async def test_action_multimodal_llm_chat_claude_image_rerun_happy_path_2(register_tools_fixture):
    tools = await ToolFactoryClaude.get_tools()
    assert tools is not None
    tool_defs = await ToolFactoryClaude.get_tool_definations(tools)
    assert tool_defs is not None

    action_config = ChatReqClaude(
        messages=[
            ClaudeMessageParam(role="user", content="Be a helpful assistant"),
        ],
        # model="claude-3-5-sonnet-20240620",
        tools=tools,
    )
    user = UserORM(id=str(gen_uuid()))
    db = next(get_mongo_db())
    action_doc = ActionDoc(
        id="action_doc_1",
        user_id="user_id_1",
        name="action_1",
        type=ActionType.text2text_llm_chat_claude,
        config=action_config.model_dump(exclude_none=True),
    )
    action_input = {
        "text": "Summarize this image and find a similar image",
        "urls": ["https://upload.wikimedia.org/wikipedia/en/9/9c/Archie_1942_issue_1.jpg"],
    }
    app_state = AppState()
    action_run_obj_1 = await ActionFactory.run_action(
        Context(), app_state, action_doc, action_input, user, db=db
    )
    assert action_run_obj_1.output_dict
    output_data_blocks = DataBlocks.model_validate(action_run_obj_1.output_dict)
    assert output_data_blocks.blocks[0].text.lower().__contains__("comic")
    # assert len(action_run_obj_1.config_dict.get("messages")) > 1

    action_doc.config = action_run_obj_1.config_dict
    action_doc.input = action_run_obj_1.input_dict
    action_doc.output = action_run_obj_1.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))

    action_input = {"text": "Keep the same plot but make it funny"}
    action_run_obj_2 = await ActionFactory.run_action(
        Context(), app_state, action_doc, action_input, user, db=db
    )
    assert action_run_obj_2.output_dict
    # assert len(action_run_obj_2.config_dict.get("messages")) > 3
    # assert action_run_obj_2.config_dict.get("messages")[1].get("content") == action_run_obj_1.input_dict.get("text")
    # assert action_run_obj_1.output_dict.get("texts")[0].get("text") in action_run_obj_2.config_dict.get("messages")[-2].get("content")


@pytest.mark.asyncio
@pytest.mark.skip
async def test_action_multimodal_llm_chat_func_tool_rerun_happy_path_3(register_tools_fixture):
    tools = await ToolFactoryClaude.get_tools()
    assert tools is not None
    tool_defs = await ToolFactoryClaude.get_tool_definations(tools)
    assert tool_defs is not None

    action_config = ChatReqClaude(
        messages=[
            ClaudeMessageParam(role="user", content="Be a helpful assistant"),
        ],
        # model="claude-3-5-sonnet-20240620",
        tools=["get_date_time"],
    )
    user = UserORM(id=str(gen_uuid()))
    db = next(get_mongo_db())
    action_doc = ActionDoc(
        id="action_doc_1",
        user_id="user_id_1",
        name="action_1",
        type=ActionType.text2text_llm_chat_claude,
        config=action_config.model_dump(exclude_none=True),
    )
    action_input = {"text": "What is current date & tine in San Francisco?"}
    app_state = AppState()
    action_run_obj_1 = await ActionFactory.run_action(
        Context(), app_state, action_doc, action_input, user, db=db
    )
    assert action_run_obj_1.output_dict
    output_data_blocks = DataBlocks.model_validate(action_run_obj_1.output_dict)
    assert output_data_blocks.blocks[0].text.lower().__contains__("date")
    # assert len(action_run_obj_1.config_dict.get("messages")) > 1

    action_doc.config = action_run_obj_1.config_dict
    action_doc.input = action_run_obj_1.input_dict
    action_doc.output = action_run_obj_1.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))

    action_input = {"text": "Keep the same plot but make it funny"}
    action_run_obj_2 = await ActionFactory.run_action(
        Context(), app_state, action_doc, action_input, user, db=db
    )
    assert action_run_obj_2.output_dict
    # assert len(action_run_obj_2.config_dict.get("messages")) > 3
    # assert action_run_obj_2.config_dict.get("messages")[1].get("content") == action_run_obj_1.input_dict.get("text")
    # assert action_run_obj_1.output_dict.get("texts")[0].get("text") in action_run_obj_2.config_dict.get("messages")[-2].get("content")
