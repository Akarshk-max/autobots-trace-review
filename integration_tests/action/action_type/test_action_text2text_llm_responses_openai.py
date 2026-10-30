from typing import Literal

import pytest
from loguru import logger
from openai.types.responses import ResponseInputImageParam, ResponseInputTextParam
from openai.types.responses.response_input_item_param import Message

from src.autobots.action.action.action_doc_model import ActionDoc, ActionResult
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_text2text.action_text2text_llm_responses_openai import (
    LlmResponsesOpenaiConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["o4-mini"])
@pytest.mark.parametrize("store", ["true", "false"])
@pytest.mark.parametrize(
    "provider",
    [
        "openai",
        # "azure"
    ],
)
async def test_action_openai_responses_function_calling_happy_path(
    ctx,
    db,
    user,
    store: bool,
    provider: Literal["azure", "openai"],
    model,
    set_test_settings,
    register_tools_fixture,
):
    # Initial configuration with a system message
    tools = ["google_search_main"]
    action_config = LlmResponsesOpenaiConfig(
        provider=provider,
        input=[
            Message(
                content=[
                    ResponseInputTextParam(
                        text="Be a helpful assistant. Use tools to complete user request",
                        type="input_text",
                    )
                ],
                role="system",
            ),
            # {
            #     "role": "system",
            #     "content": "Be a helpful assistant. Use tools to complete user request",
            # }
        ],
        model=model,
        tools=tools,
        # These parameters will be used if Responses API is available, otherwise ignored
        store=store,
        truncation="auto",
        tool_output_text_to_file_char_limit=100,
    )

    # Create action document
    action_doc = ActionDoc(
        id="action_doc_1",
        user_id=user.id,
        name="action_1",
        type=ActionType.text2text_llm_responses_openai,
        config=action_config.model_dump(exclude_none=True),
    )

    # First interaction
    action_input = DataBlock(text="Latest news about AI")
    app_state = AppState()
    action_run_obj_1 = await ActionFactory.run_action(
        ctx, app_state, action_doc, action_input.model_dump(), user, db=db
    )
    assert action_run_obj_1.output_dict
    logger.debug(f"action run res {action_run_obj_1}")
    logger.debug(f"messages-1 {action_run_obj_1.config_dict.get('input')}")
    assert len(action_run_obj_1.config_dict.get("input")) >= 3

    action_doc.config = action_run_obj_1.config_dict
    action_doc.input = action_run_obj_1.input_dict
    action_doc.output = action_run_obj_1.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))

    action_input = {"text": "Make it funny and interesting"}
    app_state = AppState()
    action_run_obj_2 = await ActionFactory.run_action(ctx, app_state, action_doc, action_input, user, db=db)
    assert action_run_obj_2.output_dict
    assert len(action_run_obj_2.config_dict.get("input")) >= 5
    assert action_run_obj_2.config_dict.get("input")[1].get("content")[0].get(
        "text"
    ) == action_run_obj_1.input_dict.get("text")
    assert action_run_obj_1.output_dict.get("blocks")[0].get(
        "text"
    ) in action_run_obj_2.config_dict.get("input")[
        len(action_run_obj_1.config_dict.get("input")) - 1
    ].get("content")[0].get("text")

    action_doc.config = action_run_obj_2.config_dict
    action_doc.input = action_run_obj_2.input_dict
    action_doc.output = action_run_obj_2.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))

    action_input = {"text": "Search about latest Arsenal FC news"}
    action_run_obj_3 = await ActionFactory.run_action(ctx, app_state, action_doc, action_input, user, db=db)
    assert action_run_obj_3.output_dict
    assert (
        len(action_run_obj_3.config_dict.get("input"))
        > len(action_run_obj_2.config_dict.get("input")) + 1
    ), "Didnt get 2nd response from OpenAI"
    # assert action_run_obj_3.config_dict.get("input")[1].get("content") == action_run_obj_2.input_dict.get("text")
    # assert action_run_obj_3.output_dict.get("blocks")[0].get("text") in action_run_obj_3.config_dict.get("messages")[
    #     -3].get("content")


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["o4-mini"])
@pytest.mark.parametrize(
    "provider",
    [
        # "azure",
        "openai"
    ],
)
async def test_action_openai_responses_image_happy_path(
    ctx,
    db,
    user,
    provider: Literal["azure", "openai"],
    model,
    set_test_settings,
    register_tools_fixture,
):
    # Initial configuration with system message
    tools = ["google_search_main"]
    action_config = LlmResponsesOpenaiConfig(
        provider=provider,
        input=[
            Message(
                type="message",
                role="user",
                content=[
                    ResponseInputTextParam(type="input_text", text="what is in this image?"),
                    ResponseInputImageParam(
                        type="input_image",
                        image_url="https://upload.wikimedia.org/wikipedia/commons/thumb/d/dd/Gfp-wisconsin-madison-the-nature-boardwalk.jpg/2560px-Gfp-wisconsin-madison-the-nature-boardwalk.jpg",
                        detail="auto",
                    ),
                ],
            ),
        ],
        model=model,
        tools=tools,
        # These parameters will be used if Responses API is available, otherwise ignored
        store=True,
        truncation="auto",
    )

    # Create action document
    action_doc = ActionDoc(
        id="action_doc_1",
        user_id=user.id,
        name="action_1",
        type=ActionType.text2text_llm_responses_openai,
        config=action_config.model_dump(exclude_none=True),
    )

    # Setup user and database
    # user = UserORM(id=str(gen_uuid()))
    # db = next(get_mongo_db())

    # First interaction
    action_input = DataBlock(text="")
    app_state = AppState()
    action_run_obj_1 = await ActionFactory.run_action(
        ctx, app_state, action_doc, action_input.model_dump(), user, db=db
    )

    # Verify first response
    assert action_run_obj_1.output_dict
    logger.debug(f"action run res {action_run_obj_1}")
    logger.debug(f"messages-1 {action_run_obj_1.config_dict.get('input')}")
    assert len(action_run_obj_1.config_dict.get("input")) >= 3
    assert "boardwalk" in action_run_obj_1.output_dict.get("blocks")[0].get("text")


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["o4-mini"])
@pytest.mark.parametrize("store", [True, False])
@pytest.mark.parametrize(
    "provider",
    [
        "openai",
        # "azure",
    ],
)
async def test_action_openai_responses_file_search_happy_path(
    ctx,
    db,
    user,
    provider: Literal["azure", "openai"],
    store: bool,
    model,
    set_test_settings,
    register_tools_fixture,
):
    # Initial configuration with system message
    tools = ["file_search"]
    action_config = LlmResponsesOpenaiConfig(
        provider=provider,
        input=[
            Message(
                type="message",
                role="user",
                content=[
                    ResponseInputTextParam(type="input_text", text="You are a helpful assistant."),
                ],
            ),
        ],
        model=model,
        tools=tools,
        # These parameters will be used if Responses API is available, otherwise ignored
        store=store,
        truncation="auto",
        # vector_store_ids=["vs_2DSEddlfKWZvFbMRdLKxeoBY"]
    )

    # Create action document
    action_doc = ActionDoc(
        id="action_doc_1",
        user_id=user.id,
        name="action_1",
        type=ActionType.text2text_llm_responses_openai,
        config=action_config.model_dump(exclude_none=True),
    )

    # First interaction
    action_input = DataBlock(
        text='I have uploaded 2 papers related to "Attention" and "MICROADAM". What are the title and Who are authors on each of the papers',
        urls=[
            f"s3://app-myautobots-local/files/{user.id}/NIPS-2017-attention-is-all-you-need-Paper.pdf",
            "https://proceedings.neurips.cc/paper_files/paper/2024/file/000f947dcaff8fbffcc3f53a1314f358-Paper-Conference.pdf",
        ],
    )

    app_state = AppState()
    action_run_obj_1 = await ActionFactory.run_action(
        ctx, app_state, action_doc, action_input.model_dump(), user, db=db
    )

    # Verify first response
    assert action_run_obj_1.output_dict
    logger.debug(f"action run res {action_run_obj_1}")
    logger.debug(f"messages-1 {action_run_obj_1.config_dict.get('input')}")
    assert len(action_run_obj_1.config_dict.get("input")) > len(action_config.input) + 1
    assert "Attention" in action_run_obj_1.output_dict.get("blocks")[0].get("text")
    # assert "MICROADAM" in action_run_obj_1.output_dict.get("blocks")[0].get("text")

    action_doc.config = action_run_obj_1.config_dict
    action_doc.input = action_run_obj_1.input_dict
    action_doc.output = action_run_obj_1.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))

    action_input = {"text": "Combine these 2 papers of Attention and MICROADAM"}
    app_state = AppState()
    action_run_obj_2 = await ActionFactory.run_action(ctx, app_state, action_doc, action_input, user, db=db)
    assert action_run_obj_2.output_dict
    assert (
        len(action_run_obj_2.config_dict.get("input"))
        > len(action_run_obj_1.config_dict.get("input")) + 1
    ), "Didnt get 2nd response from OpenAI"
    # assert action_run_obj_2.config_dict.get("input")[1].get("content") == action_run_obj_1.input_dict.get("text")
    # assert action_run_obj_1.output_dict.get("blocks")[0].get("text") in action_run_obj_2.config_dict.get("messages")[
    #     -3].get("content")

    action_doc.config = action_run_obj_2.config_dict
    action_doc.input = action_run_obj_2.input_dict
    action_doc.output = action_run_obj_2.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))

    action_input = {"text": "Tell me more about references used in these papers"}
    action_run_obj_3 = await ActionFactory.run_action(ctx, app_state, action_doc, action_input, user, db=db)
    assert action_run_obj_3.output_dict
    assert (
        len(action_run_obj_3.config_dict.get("input"))
        > len(action_run_obj_2.config_dict.get("input")) + 1
    ), "Didnt get 2nd response from OpenAI"
    # assert action_run_obj_3.config_dict.get("input")[1].get("content") == action_run_obj_2.input_dict.get("text")
    # assert action_run_obj_3.output_dict.get("blocks")[0].get("text") in action_run_obj_3.config_dict.get("messages")[
    #     -3].get("content")


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["o4-mini"])
@pytest.mark.parametrize("store", [True, False])
@pytest.mark.parametrize(
    "provider",
    [
        # "azure", # "Hosted tool 'code_interpreter' is not supported with o4-mini-2025-04-16."
        "openai"
    ],
)
async def test_action_openai_responses_code_interpreter_happy_path(
    ctx,
    db,
    user,
    provider: Literal["azure", "openai"],
    store: bool,
    model,
    set_test_settings,
    register_tools_fixture,
):
    # Initial configuration with system message
    tools = ["code_interpreter"]
    action_config = LlmResponsesOpenaiConfig(
        provider=provider,
        input=[
            Message(
                type="message",
                role="user",
                content=[
                    ResponseInputTextParam(
                        type="input_text", text="You are a helpful assistant. Use code interpreter"
                    ),
                ],
            ),
        ],
        model=model,
        tools=tools,
        # These parameters will be used if Responses API is available, otherwise ignored
        store=store,
        truncation="auto",
        # vector_store_ids=["vs_2DSEddlfKWZvFbMRdLKxeoBY"]
    )

    # Create action document
    action_doc = ActionDoc(
        id="action_doc_1",
        user_id=user.id,
        name="action_1",
        type=ActionType.text2text_llm_responses_openai,
        config=action_config.model_dump(exclude_none=True),
    )

    # First interaction
    action_input = DataBlock(
        # text="Explain this CSV and put it in a table format:",
        # urls=[
        #     "https://gist.githubusercontent.com/armgilles/194bcff35001e7eb53a2a8b441e8b2c6/raw/92200bc0a673d5ce2110aaad4544ed6c4010f687/pokemon.csv",
        #     # "https://app-myautobots-public-local.s3.us-west-2.amazonaws.com/files/9f6ab403-3d77-407e-912b-2438f09487d7/pokemon.csv",
        # ],
        text="Generate a csv of pokemon characters with their stats and types"
    )
    app_state = AppState()
    action_run_obj_1 = await ActionFactory.run_action(
        ctx, app_state, action_doc, action_input.model_dump(), user, db=db
    )
    assert action_run_obj_1.output_dict
    logger.debug(f"action run res {action_run_obj_1}")
    logger.debug(f"messages-1 {action_run_obj_1.config_dict.get('input')}")
    assert len(action_run_obj_1.config_dict.get("input")) > len(action_config.input) + 1
    assert len(action_run_obj_1.config_dict["file_url_to_file_object_map"]) > 0
    assert "Pokémon" in action_run_obj_1.output_dict.get("blocks")[0].get("text")

    action_doc.config = action_run_obj_1.config_dict
    action_doc.input = action_run_obj_1.input_dict
    action_doc.output = action_run_obj_1.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))

    action_input = {"text": "Which is the best character"}
    app_state = AppState()
    action_run_obj_2 = await ActionFactory.run_action(ctx, app_state, action_doc, action_input, user, db=db)
    assert action_run_obj_2.output_dict
    assert (
        len(action_run_obj_2.config_dict.get("input"))
        > len(action_run_obj_1.config_dict.get("input")) + 1
    ), "Didnt get 2nd response from OpenAI"
    # assert action_run_obj_2.config_dict.get("input")[1].get("content") == action_run_obj_1.input_dict.get("text")
    # assert action_run_obj_1.output_dict.get("blocks")[0].get("text") in action_run_obj_2.config_dict.get("messages")[
    #     -3].get("content")

    action_doc.config = action_run_obj_2.config_dict
    action_doc.input = action_run_obj_2.input_dict
    action_doc.output = action_run_obj_2.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))

    action_input = {"text": "Tell me more about best pokemon"}
    action_run_obj_3 = await ActionFactory.run_action(ctx, app_state, action_doc, action_input, user, db=db)
    assert action_run_obj_3.output_dict
    assert (
        len(action_run_obj_3.config_dict.get("input"))
        > len(action_run_obj_2.config_dict.get("input")) + 1
    ), "Didnt get 3rd response from OpenAI"
    # assert action_run_obj_3.config_dict.get("input")[1].get("content") == action_run_obj_2.input_dict.get("text")
    # assert action_run_obj_3.output_dict.get("blocks")[0].get("text") in action_run_obj_3.config_dict.get("messages")[
    #     -3].get("content")


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["o4-mini"])
@pytest.mark.parametrize("store", [True, False])
@pytest.mark.parametrize(
    "provider",
    [
        # "azure", # "Hosted tool 'code_interpreter' is not supported with o4-mini-2025-04-16."
        "openai"
    ],
)
async def test_action_llm_chat_openai_responses_with_web_search(
    ctx,
    db,
    user,
    provider: Literal["azure", "openai"],
    store: bool,
    model,
    set_test_settings,
    register_tools_fixture,
):
    # Configuration with web search tool
    # This test will use the chat.completions API if the responses API is not available
    action_config = LlmResponsesOpenaiConfig(
        provider=provider,
        input=[
            {"role": "system", "content": "You are a helpful assistant that can search the web."},
            {"role": "user", "content": "What's the latest news about AI?"},
        ],
        model=model,
        tools=["web_search"],
        # These parameters will be used if Responses API is available, otherwise ignored
        store=store,
        truncation="auto",
    )

    # Create action document
    action_doc = ActionDoc(
        id="action_doc_3",
        user_id=user.id,
        name="action_3",
        type=ActionType.text2text_llm_responses_openai,
        config=action_config.model_dump(exclude_none=True),
    )

    # Run action with web search
    action_input = DataBlock(text="Tell me about recent AI developments")
    app_state = AppState()
    action_run_obj = await ActionFactory.run_action(
        Context(), app_state, action_doc, action_input.model_dump(), user, db=db
    )

    # Verify response
    assert action_run_obj.output_dict
    assert len(action_run_obj.config_dict.get("input")) > 1
    assert action_run_obj.config_dict.get("tools") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["o4-mini"])
@pytest.mark.parametrize("store", ["true", "false"])
@pytest.mark.parametrize(
    "provider",
    [
        "openai",
        # "azure"
    ],
)
async def test_action_openai_responses_sandbox_file_happy_path(
    ctx,
    db,
    user,
    store: bool,
    provider: Literal["azure", "openai"],
    model,
    set_test_settings,
    register_tools_fixture,
):
    # Initial configuration with system message
    tools = ["google_search_main", "file_search", "code_interpreter"]
    action_config = LlmResponsesOpenaiConfig(
        provider=provider,
        input=[
            {
                "role": "system",
                "content": "Be a helpful assistant. Use tools to complete user request",
            }
        ],
        model=model,
        tools=tools,
        # These parameters will be used if Responses API is available, otherwise ignored
        store=store,
        truncation="auto",
    )

    # Create action document
    action_doc = ActionDoc(
        id="action_doc_1",
        user_id=user.id,
        name="action_1",
        type=ActionType.text2text_llm_responses_openai,
        config=action_config.model_dump(exclude_none=True),
    )

    # Setup user and database
    # user = UserORM(id=str(gen_uuid()))
    # db = next(get_mongo_db())

    # First interaction
    action_input = DataBlock(
        text="You are a personal math tutor. When asked a math question, write and run code using the python tool to answer the question. Plot the function sin(x) from -10 to 10 with a little bit of noise"
    )
    app_state = AppState()
    action_run_obj_1 = await ActionFactory.run_action(
        ctx, app_state, action_doc, action_input.model_dump(), user, db=db
    )
    assert action_run_obj_1.output_dict
    logger.debug(f"action run res {action_run_obj_1}")
    logger.debug(f"messages-1 {action_run_obj_1.config_dict.get('input')}")
    assert len(action_run_obj_1.config_dict.get("input")) > len(action_config.input) + 1

    action_doc.config = action_run_obj_1.config_dict
    action_doc.input = action_run_obj_1.input_dict
    action_doc.output = action_run_obj_1.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))
    if action_input.metadata.process_attachment_url:
        assert action_run_obj_1.output_dict.get("blocks")[0].get("urls")
        assert len(action_run_obj_1.output_dict.get("blocks")[0].get("urls")) > 0

    action_input = {"text": "Make it funny and interesting and convert it to audio"}
    app_state = AppState()
    action_run_obj_2 = await ActionFactory.run_action(ctx, app_state, action_doc, action_input, user, db=db)
    assert action_run_obj_2.output_dict
    assert (
        len(action_run_obj_2.config_dict.get("input"))
        > len(action_run_obj_1.config_dict.get("input")) + 1
    ), "Didnt get 2nd response from OpenAI"
    assert action_run_obj_2.config_dict.get("input")[1].get("content")[0].get(
        "text"
    ) == action_run_obj_1.input_dict.get("text")
    assert action_run_obj_1.output_dict.get("blocks")[0].get(
        "text"
    ) in action_run_obj_2.config_dict.get("input")[
        len(action_run_obj_1.config_dict.get("input")) - 1
    ].get("content")[0].get("text")

    action_doc.config = action_run_obj_2.config_dict
    action_doc.input = action_run_obj_2.input_dict
    action_doc.output = action_run_obj_2.output_dict
    action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))

    action_input = {"text": "Create cover art for the audio file"}
    action_run_obj_3 = await ActionFactory.run_action(ctx, app_state, action_doc, action_input, user, db=db)
    assert action_run_obj_3.output_dict
    assert (
        len(action_run_obj_3.config_dict.get("input"))
        > len(action_run_obj_2.config_dict.get("input")) + 1
    ), "Didnt get 3rd response from OpenAI"
    # assert action_run_obj_3.config_dict.get("input")[1].get("content") == action_run_obj_2.input_dict.get("text")
    # assert action_run_obj_3.output_dict.get("blocks")[0].get("text") in action_run_obj_3.config_dict.get("messages")[
    #     -3].get("content")


@pytest.mark.asyncio
@pytest.mark.skip("Streaming test requires special handling")
async def test_action_llm_chat_openai_responses_streaming(
    set_test_settings, register_tools_fixture
):
    _ = Context()

    # Configuration with streaming enabled
    action_config = LlmResponsesOpenaiConfig(
        provider="azure",
        input=[{"role": "user", "content": "Write a short story"}],
        model="gpt-4o",
        stream=True,
        # These parameters will be used if Responses API is available, otherwise ignored
        store=True,
        truncation="auto",
    )

    # Create action document
    action_doc = ActionDoc(
        id="action_doc_4",
        user_id="user_id_4",
        name="action_4",
        type=ActionType.text2text_llm_responses_openai,
        config=action_config.model_dump(exclude_none=True),
    )

    # Setup user and database
    user = UserORM(id=str(gen_uuid()))
    db = next(get_mongo_db())
    app_state = AppState()

    # Run action with streaming
    action_input = DataBlock(text="Make it about a space adventure")
    action_run_obj = await ActionFactory.run_action(
        Context(), app_state, action_doc, action_input.model_dump(), user, db=db
    )

    # Verify streaming response
    assert action_run_obj.output_dict
    assert len(action_run_obj.config_dict.get("messages")) > 1
    assert action_run_obj.config_dict.get("stream") is True


@pytest.mark.asyncio
@pytest.mark.skip("Streaming test requires special handling")
async def test_action_openai_responses_config_happy_path(set_test_settings, register_tools_fixture):
    try:
        config_dict = {
            "input": [
                {
                    "content": "You are a helpful assistant. Always use tools before answering.",
                    "role": "system",
                },
                {
                    "content": [
                        {"text": "Create an excel with the days in a week.", "type": "input_text"}
                    ],
                    "role": "user",
                    "type": "message",
                },
                {
                    "id": "rs_687df78f2da8819aa8b29ee5838e64ea081180c71283b809",
                    "summary": [],
                    "type": "reasoning",
                },
                {
                    "id": "ci_687df7936350819aa169d9256ae22db7081180c71283b809",
                    "code": 'import pandas as pd\n\n# Create a DataFrame with days of the week\ndays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]\ndf = pd.DataFrame({"Day": days})\n\n# Save to an Excel file\nfile_path = "/mnt/data/days_in_week.xlsx"\ndf.to_excel(file_path, index=False)\n\n# Display the DataFrame\ndf',
                    "outputs": None,
                    "container_id": "cntr_687df78e19a88198ace5eaa08c6f01b608ca9e3b2f206959",
                    "status": "completed",
                    "type": "code_interpreter_call",
                },
                {
                    "id": "msg_687df7981f28819a8ce48791592d0069081180c71283b809",
                    "content": [
                        {
                            "annotations": [
                                {
                                    "container_id": "cntr_687df78e19a88198ace5eaa08c6f01b608ca9e3b2f206959",
                                    "end_index": 163,
                                    "file_id": "cfile_687df79ac01c81918132080bf4d33f22",
                                    "filename": "days_in_week.xlsx",
                                    "start_index": 128,
                                    "type": "container_file_citation",
                                }
                            ],
                            "text": "I’ve created the Excel file with the days of the week. You can download it here:\n\n[Download the Excel file (days_in_week.xlsx)](sandbox:/mnt/data/days_in_week.xlsx)",
                            "type": "output_text",
                            "logprobs": [],
                        }
                    ],
                    "role": "assistant",
                    "status": "completed",
                    "type": "message",
                },
            ],
            "model": "o4-mini",
            "reasoning": {"effort": "high", "generate_summary": None, "summary": None},
            "tools": [{"container": {"type": "auto"}, "type": "code_interpreter"}],
            "provider": "openai",
            "vector_store_ids": [],
        }

        config = LlmResponsesOpenaiConfig.model_validate(config_dict)
        assert config
    except Exception as e:
        raise AssertionError(f"Failed to validate config: {e}")
