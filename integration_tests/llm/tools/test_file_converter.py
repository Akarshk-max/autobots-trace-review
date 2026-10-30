import pytest
from fastapi import UploadFile
from openai.types.chat import ChatCompletionUserMessageParam
from pydantic import HttpUrl

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock, TextObj
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    LlmChatOpenaiConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.files.file_converter import (
    CSV_XLSheetToTxtConfig,
    FileConverter,
    FileConverterConfig,
)
from src.autobots.files.user_files import UserFiles
from src.autobots.llm.tools.tool_factory import ToolFactory


@pytest.mark.asyncio
# @pytest.mark.parametrize("file_url", [
#     "https://app-myautobots-public-local.s3.us-west-2.amazonaws.com/files/test_user_id/pokemon.csv",
#     "https://app-myautobots-public-local.s3.us-west-2.amazonaws.com/files/test_user_id/pokemon.xlsx",
#     "https://app-myautobots-public-local.s3.us-west-2.amazonaws.com/files/test_user_id/pokemon.docx"
#     # FAILING: "https://app-myautobots-public-local.s3.us-west-2.amazonaws.com/files/test_user_id/pokemon.pptx",
# ])
@pytest.mark.parametrize(
    "file_name",
    [
        "pokemon.csv",
        "pokemon.xlsx",
        "pokemon.docx",
        # FAILING: "pokemon.pptx",
    ],
)
async def test_csv_xls_sheet_to_txt_happy_path_1(ctx, db, user, file_name, set_test_settings):
    s3 = get_public_s3()
    user_files = UserFiles(user=user, s3=s3)
    list_files = await user_files.list_files(ctx, filename=file_name)
    file_url: str = ""
    if len(list_files) > 0:
        file_url = str(list_files[0].url)
    else:
        file_path = f"tests/resources/files/{file_name}"
        with open(file_path, mode="rb") as file:
            upload_file = UploadFile(filename=file_name, file=file)
            # content = file.read()
            file_metas = await user_files.upload_files(ctx, [upload_file])
            file_url = str(file_metas[0].url)
    assert file_url

    config = CSV_XLSheetToTxtConfig(url=file_url)
    text_obj: TextObj = await FileConverter.csv_xls_doc_ppt_to_txt(
        config=config, ctx=ctx, user=user
    )
    assert text_obj.text
    assert text_obj.text.__contains__("Pikachu")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_name",
    [
        "pokemon.csv",
        "pokemon.xlsx",
        "pokemon.docx",
        # FAILING: "pokemon.pptx",
    ],
)
async def test_csv_xls_sheet_to_txt_file_happy_path_1(ctx, db, user, file_name, set_test_settings):
    file_path = f"tests/resources/files/{file_name}"
    config = FileConverterConfig(path=file_path)
    file_url: HttpUrl = await FileConverter.convert_file_to_txt_file(
        config=config, ctx=ctx, user=user
    )
    assert file_url
    assert file_url.path.endswith(".txt")


@pytest.mark.asyncio
async def test_action_csv_xls_sheet_to_txt_run_happy_path(
    ctx, db, user, set_test_settings, register_tools_fixture
):
    # Initialize context, DB, and available tools
    tools = await ToolFactory.get_tools()
    assert tools is not None
    tool_defs = await ToolFactory.get_chat_completion_tool_param(ctx=ctx, tool_names=tools)
    assert tool_defs is not None

    # Prepare the OpenAI Chat request to invoke our tool
    action_config = LlmChatOpenaiConfig(
        provider="azure",
        messages=[
            ChatCompletionUserMessageParam(
                role="user",
                content="Be a helpful assistant. Convert it to text format using tool, csv_xls_doc_ppt_to_txt.",
            )
        ],
        model="gpt-4.1-mini",
        tools=tools,
    )

    # Create the ActionDoc for csv_xls_sheet_to_txt
    action_doc = ActionDoc(
        id="action_doc_csv_1",
        user_id=user.id,
        name="csv_xls_sheet_to_txt",
        type=ActionType.text2text_llm_chat_openai,
        config=action_config.model_dump(exclude_none=True),
    )

    # Use a DataBlock carrying the URL
    action_input = DataBlock(
        urls=[
            "https://app-myautobots-public-dev.s3.amazonaws.com/files/e29f3392-6124-40f5-8e24-fd6b5ebff875/mnt_data_India_data-ba65eacc-0de2-4595-8141-3916bfe6ece7.docx"
        ]
    )

    # Run the action
    action_run_obj = await ActionFactory.run_action(
        Context(), AppState(), action_doc, action_input.model_dump(), user, db=db
    )

    # Assertions: expect some text output
    assert action_run_obj.output_dict
    print("Output Dictionary:", action_run_obj.output_dict)
    final_block = action_run_obj.output_dict.get("blocks")[-1]
    print("Final Block:", final_block)
    text_output = final_block.get("text")
    assert isinstance(text_output, str)
    assert len(text_output) > 0
