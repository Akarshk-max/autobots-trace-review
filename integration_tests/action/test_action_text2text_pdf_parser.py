import json
from typing import Literal

import pytest
from openai.types.chat import ChatCompletionUserMessageParam

from src.autobots.action.action.action_doc_model import ActionDoc, ActionResult
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    LlmChatOpenaiConfig,
)
from src.autobots.action.action_type.action_text2text.action_text2text_pdf_parser import (
    ActionPDFParser,
    PDFParserReq,
    PDFParserRes,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.tool_factory import ToolFactory

# Path to a test PDF file with known content
TEST_PDF_PATH = "https://app-myautobots-public-dev.s3.amazonaws.com/files/e29f3392-6124-40f5-8e24-fd6b5ebff875/Khushal_CV_cs_11.pdf"
# Replace with the path to your test PDF file
EXPECTED_TEXT = "khushal"  # Replace with the expected text from the PDF


@pytest.mark.asyncio
async def test_action_pdf_parser_run_action(ctx, db, user, set_test_settings):
    # Setup
    # use_ocr = False
    action_input = DataBlock(urls=[TEST_PDF_PATH])
    action_config = PDFParserReq(pdf_url=TEST_PDF_PATH, use_ocr=False)
    action_pdf_parser = ActionPDFParser(action_config=action_config, db=db, user=user)

    # Execute
    app_state = AppState()
    result = await action_pdf_parser.run_action(ctx, app_state, action_input)

    # Verify
    assert result is not None
    assert isinstance(result, DataBlocks)
    assert len(result.blocks) > 0
    assert len(result.blocks[0].text) > 0
    assert EXPECTED_TEXT.strip() in result.blocks[0].text, (
        f"Expected text not found in extracted text.\n"
        f"Expected: {EXPECTED_TEXT.strip()}\n"
        f"Extracted: {result.extracted_text}"
    )


@pytest.mark.asyncio
async def test_action_pdf_parser_run_tool(ctx, db, user):
    action_config = PDFParserReq(pdf_url=TEST_PDF_PATH, use_ocr=False)
    action_pdf_parser = ActionPDFParser(action_config=action_config, db=db, user=user)
    assert isinstance(action_pdf_parser, ActionPDFParser)
    # Execute
    app_state = AppState()
    result = await action_pdf_parser.run_tool(action_config, ctx, app_state)

    # Verify
    assert result is not None
    assert isinstance(result, PDFParserRes)
    assert len(result.extracted_text) > 0
    # assert result.extracted_text.strip() == EXPECTED_TEXT.strip()  # Check if the extracted text matches the expected text
    assert EXPECTED_TEXT.strip() in result.extracted_text, (
        f"Expected text not found in extracted text.\n"
        f"Expected: {EXPECTED_TEXT.strip()}\n"
        f"Extracted: {result.extracted_text}"
    )


@pytest.mark.asyncio
async def test_action_pdf_parser_run_action_with_ocr(ctx, db, user, set_test_settings):
    # Setup
    # use_ocr = True
    action_input = DataBlock(urls=[TEST_PDF_PATH])
    # action_input = PDFParserReq(pdf_path=TEST_PDF_PATH, use_ocr=use_ocr)
    action_config = PDFParserReq(pdf_url=TEST_PDF_PATH, use_ocr=True)
    action_pdf_parser = ActionPDFParser(action_config=action_config, db=db, user=user)

    # Execute
    app_state = AppState()
    result = await action_pdf_parser.run_action(ctx, app_state, action_input)

    # Verify
    assert result is not None
    assert isinstance(result, DataBlocks)
    assert len(result.blocks) > 0
    assert len(result.blocks[0].text) > 0
    # assert result.extracted_text.strip() == EXPECTED_TEXT.strip()  # Check if the extracted text matches the expected text
    assert EXPECTED_TEXT.strip() in result.blocks[0].text, (
        f"Expected text not found in extracted text.\n"
        f"Expected: {EXPECTED_TEXT.strip()}\n"
        f"Extracted: {result.extracted_text}"
    )


@pytest.mark.asyncio
async def test_action_pdf_parser_run_tool_with_ocr(ctx, db, user, set_test_settings):
    # Setup
    action_config = PDFParserReq(pdf_url=TEST_PDF_PATH, use_ocr=True)
    action_pdf_parser = ActionPDFParser(action_config=action_config, db=db, user=user)
    assert isinstance(action_pdf_parser, ActionPDFParser)
    app_state = AppState()
    result = await action_pdf_parser.run_tool(action_config, ctx, app_state)

    assert result is not None
    assert isinstance(result, PDFParserRes)
    assert len(result.extracted_text) > 0
    # assert result.extracted_text.strip() == EXPECTED_TEXT.strip()  # Check if the extracted text matches the expected text
    assert EXPECTED_TEXT.strip() in result.extracted_text, (
        f"Expected text not found in extracted text.\n"
        f"Expected: {EXPECTED_TEXT.strip()}\n"
        f"Extracted: {result.extracted_text}"
    )


@pytest.mark.asyncio
async def test_action_pdf_parser_run_action_invalid_pdf(ctx, db, user):
    # Setup

    pdf_path = "invalid.pdf"  # Non-existent PDF file
    # use_ocr = False
    action_input = DataBlock(urls=[pdf_path])
    ctx = Context()
    action_config = PDFParserReq(pdf_url=TEST_PDF_PATH, use_ocr=True)
    action_pdf_parser = ActionPDFParser(action_config=action_config, db=db, user=user)

    # Execute
    app_state = AppState()
    result = await action_pdf_parser.run_action(ctx, app_state, action_input)
    assert result is not None
    assert isinstance(result, Exception), "Expected an Exception object to be returned"


@pytest.mark.asyncio
async def test_action_pdf_parser_run_tool_invalid_pdf(ctx, db, user):
    # Setup
    pdf_path = "invalid.pdf"  # Non-existent PDF file
    use_ocr = False
    action_config = PDFParserReq(pdf_url=pdf_path, use_ocr=use_ocr)
    ctx = Context()
    action_pdf_parser = ActionPDFParser(action_config=action_config, db=db, user=user)

    # Execute
    app_state = AppState()
    result = await action_pdf_parser.run_tool(action_config, ctx, app_state)
    assert result is not None
    assert isinstance(result, Exception), "Expected an Exception object to be returned"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-4.1-mini"])
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_action_llm_chat_pdf_parser_tool_integration(
    ctx,
    db,
    user,
    model,
    provider: Literal["azure", "openai"],
    set_test_settings,
    register_tools_fixture,
):
    # Get registered tools
    tools = await ToolFactory.get_tools()
    assert tools is not None
    tool_defs = await ToolFactory.get_chat_completion_tool_param(ctx=ctx, tool_names=tools)
    assert tool_defs is not None

    action_config = LlmChatOpenaiConfig(
        provider=provider,
        messages=[
            ChatCompletionUserMessageParam(
                role="user",
                content="Extract text from this PDF document with url as :https://app-myautobots-public-dev.s3.amazonaws.com/files/e29f3392-6124-40f5-8e24-fd6b5ebff875/Khushal_CV_cs_11.pdf. Send the pdf path to the tool.",
            )
        ],
        model=model,
        tools=tools,
    )

    # Define the action document
    action_doc = ActionDoc(
        id="action_doc_pdf_parser",
        user_id=user.id,
        name="pdf_parser_action",
        type=ActionType.text2text_llm_chat_openai,
        config=action_config.model_dump(exclude_none=True),
    )

    # Define the action input
    # action_input = PDFParserReq(pdf_path=TEST_PDF_PATH, use_ocr=False)
    action_input = DataBlock(
        text="Extract text from this PDF document with path as : https://app-myautobots-public-dev.s3.amazonaws.com/files/e29f3392-6124-40f5-8e24-fd6b5ebff875/Khushal_CV_cs_11.pdf. Send the the pdf path to the tool."
    )
    # Run the action through the ActionFactory
    action_run_obj = await ActionFactory.run_action(
        ctx, AppState(), action_doc, action_input.model_dump(), user, db=db
    )

    # Verify response
    assert action_run_obj.output_dict

    print("Output Dictionary:")
    print(action_run_obj.output_dict)

    messages = action_run_obj.config_dict.get("messages")
    extracted_text = None
    for message in messages:
        if message.get("role") == "tool" and "extracted_text" in message.get("content", ""):
            # Extract the extracted_text from the content (it's stored as part of the JSON string)
            content = message.get("content")
            # Find the "extracted_text" key in the content and parse the JSON
            try:
                content_dict = json.loads(content)  # Assuming the content is a JSON string
                extracted_text = content_dict.get("extracted_text")
            except json.JSONDecodeError:
                extracted_text = None

    # Ensure extracted_text is found
    assert extracted_text is not None and len(extracted_text) > 0, "No extracted text found."

    # Check if the required text is in the extracted_text
    assert "khushal" in extracted_text, (
        f"Expected text not found in extracted text.\nExtracted: {extracted_text}"
    )
    # Update action document with results
    action_doc.config = action_run_obj.config_dict
    action_doc.input = action_run_obj.input_dict
    action_doc.output = action_run_obj.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))

    print(action_run_obj.output_dict)
