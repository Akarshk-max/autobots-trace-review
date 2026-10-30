from typing import List

import pytest
from fastapi import UploadFile
from google.genai.types import Content, Part

from src.autobots.action.action.action_doc_model import ActionDoc, ActionResult
from src.autobots.action.action.common_action_models import DataBlocks
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_gemini import (
    GeminiChatConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.gemini.gemini_model import ChatConfigGemini
from src.autobots.data_model.app_state import AppState
from src.autobots.files.user_files import UserFiles
from src.autobots.files.user_files_model import FileMeta
from src.autobots.llm.tools.tool_factory_gemini import ToolFactoryGemini


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["sample.mp4"])
async def test_action_text2text_llm_chat_gemini_with_file_happy_path(
    ctx,
    user,
    db,
    filename,
    set_test_settings,
):
    url = None
    s3 = get_public_s3()
    user_files = UserFiles(user=user, s3=s3)
    file_metas: List[FileMeta] = await user_files.list_files(ctx, filename=filename)
    if file_metas and len(file_metas) > 0:
        file_meta = file_metas[0]
        url = str(file_meta.url)
    else:
        path = f"tests/resources/files/{filename}"
        with open(path, "rb") as f:
            upload_file = UploadFile(filename=filename, file=f)
            file_metas: List[FileMeta] = await user_files.upload_files(ctx=ctx, files=[upload_file])
            assert file_metas is not None and len(file_metas) > 0, "Failed to upload file to S3"
            file_meta = file_metas[0]
            url = str(file_meta.url)

    action_config = GeminiChatConfig(
        contents=[Content(role="user", parts=[Part(text="Summarize the file")])]
    )
    action_doc = ActionDoc(
        id="action_doc_1",
        user_id="user_id_1",
        name="action_1",
        type=ActionType.text2text_llm_chat_gemini,
        config=action_config.model_dump(exclude_none=True),
    )
    action_input = {
        "text": "Describe and Analyze video and extract key actions",
        "urls": [url],
    }

    action_run_obj_1 = await ActionFactory.run_action(
        ctx=ctx, app_state=AppState(), action_doc=action_doc, action_input_dict=action_input, user=user, db=db
    )
    assert action_run_obj_1.output_dict
    datablocks = DataBlocks.model_validate(action_run_obj_1.output_dict)
    assert "purple" in datablocks.blocks[0].text.lower()

    action_doc.config = action_run_obj_1.config_dict
    action_doc.input = action_run_obj_1.input_dict
    action_doc.output = action_run_obj_1.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))

    action_input = {"text": "Generate a new pdf for the above summary"}
    app_state = AppState()
    action_run_obj_2 = await ActionFactory.run_action(ctx, app_state, action_doc, action_input, user, db=db)
    assert action_run_obj_2.output_dict
    assert (
        len(action_run_obj_2.config_dict.get("contents"))
        - len(action_run_obj_1.config_dict.get("contents"))
        >= 2
    ), "Didnt get 2nd response from Gemini"
    assert (
        # action_run_obj_1.output_dict["blocks"][0].get("text")[20:120]
        # in str(action_run_obj_2.config_dict.get("contents")[-3]["parts"][0])
        action_run_obj_1.output_dict["blocks"][0].get("text")[20:120]
        in action_run_obj_2.config_dict.get("contents")[-3]["parts"][0]["text"]
    ), "Didnt get 2nd response from Gemini as expected"


@pytest.mark.asyncio
async def test_action_text2text_llm_chat_gemini_with_tools_happy_path(
    ctx,
    user,
    db,
    set_test_settings,
):
    tool_factory = ToolFactoryGemini(user=user, db=db)
    tools = await tool_factory.get_tools()
    assert tools
    # tools += ["code_execution", "google_search", "google_maps"]
    gemini_config = ChatConfigGemini(
        tools=tools
        # tools=["llm_chat"]
        # tools=["code_execution"]
    )
    action_config = GeminiChatConfig(
        contents=[
            Content(
                role="user",
                parts=[
                    Part(text="You are a helpful assistant. Use tools to complete user request")
                ],
            )
        ],
        config=gemini_config,
    )
    action_doc = ActionDoc(
        id="action_doc_1",
        user_id="user_id_1",
        name="action_1",
        type=ActionType.text2text_llm_chat_gemini,
        config=action_config.model_dump(exclude_none=True),
    )
    action_input = {
        "text": "Get the current date and time then use tool google_search_main to find out what is the latest Arsenal news",
        # "text": "Generate a pdf with days of the week",
    }

    action_run_obj_1 = await ActionFactory.run_action(
        ctx=ctx, app_state=AppState(), action_doc=action_doc, action_input_dict=action_input, user=user, db=db
    )
    assert action_run_obj_1.output_dict
    datablocks = DataBlocks.model_validate(action_run_obj_1.output_dict)
    assert "arsenal" in datablocks.blocks[0].text.lower()

    action_doc.config = action_run_obj_1.config_dict
    action_doc.input = action_run_obj_1.input_dict
    action_doc.output = action_run_obj_1.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))

    action_input = {"text": "Generate a new pdf for the above summary"}
    app_state = AppState()
    action_run_obj_2 = await ActionFactory.run_action(ctx, app_state, action_doc, action_input, user, db=db)
    assert action_run_obj_2.output_dict
    assert (
        len(action_run_obj_2.config_dict.get("contents"))
        - len(action_run_obj_1.config_dict.get("contents"))
        >= 2
    ), "Didnt get 2nd response from Gemini"
    assert (
        action_run_obj_1.output_dict["blocks"][-1].get("text")[20:120]
        in action_run_obj_2.config_dict.get("contents")[-3]["parts"][0]["text"]
    ), "Didnt get 2nd response from Gemini as expected"


@pytest.mark.asyncio
# @pytest.mark.skip("Not successful")
async def test_action_text2text_llm_chat_gemini_with_file_generation_happy_path(
    ctx,
    user,
    db,
    set_test_settings,
):
    # tool_factory = ToolFactoryGemini(user=user, db=db)
    # tools = await tool_factory.get_tools()
    # assert tools
    gemini_config = ChatConfigGemini(
        # tools=tools
        # tools=["search_web"]
        tools=["code_execution"]
    )
    action_config = GeminiChatConfig(
        contents=[
            Content(
                role="user",
                parts=[
                    Part(text="You are a helpful assistant. Use tools to complete user request")
                ],
            )
        ],
        config=gemini_config,
    )
    action_doc = ActionDoc(
        id="action_doc_1",
        user_id="user_id_1",
        name="action_1",
        type=ActionType.text2text_llm_chat_gemini,
        config=action_config.model_dump(exclude_none=True),
    )
    action_input = {
        "text": "Generate a pdf with days of the week",
    }

    action_run_obj_1 = await ActionFactory.run_action(
        ctx=ctx, app_state=AppState(), action_doc=action_doc, action_input_dict=action_input, user=user, db=db
    )
    assert action_run_obj_1.output_dict
    datablocks = DataBlocks.model_validate(action_run_obj_1.output_dict)
    assert "week" in datablocks.blocks[-1].text.lower()

    action_doc.config = action_run_obj_1.config_dict
    action_doc.input = action_run_obj_1.input_dict
    action_doc.output = action_run_obj_1.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))

    action_input = {"text": "Generate a new pdf for the above summary"}
    app_state = AppState()
    action_run_obj_2 = await ActionFactory.run_action(ctx, app_state, action_doc, action_input, user, db=db)
    assert action_run_obj_2.output_dict
    assert (
        len(action_run_obj_2.config_dict.get("contents"))
        - len(action_run_obj_1.config_dict.get("contents"))
        >= 2
    ), "Didnt get 2nd response from Gemini"
    assert (
        action_run_obj_1.output_dict["blocks"][0].get("text")[20:120]
        in action_run_obj_2.config_dict.get("contents")[-3]["parts"][0]["text"]
    ), "Didnt get 2nd response from Gemini as expected"
