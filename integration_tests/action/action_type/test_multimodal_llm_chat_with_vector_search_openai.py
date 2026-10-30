from typing import Literal

import chromadb
import pytest
from openai.types.chat import ChatCompletionUserMessageParam

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_text2text.action_multimodal_llm_chat_with_vector_search_openai import (
    ActionCreateMultiModalLlmChatWithVectorSearchOpenaiConfig,
    Operations,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    LlmChatOpenaiConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.aws.s3 import get_s3
from src.autobots.data_model.app_state import AppState
from src.autobots.datastore.multidatastore import MultiDataStore


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider",
    [
        "azure",
        # "openai"
    ],
)
async def test_action_multimodal_llm_chat_with_vector_search_openai_rerun_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    query = "Kiwi"
    s3 = get_s3()
    settings = chromadb.config.Settings(is_persistent=True)
    settings.persist_directory = "db"
    datastore = MultiDataStore(s3, openai_provider=provider).init(id="vector_search_teststore")

    file_name = "tests/resources/unstructured_io/Runkai Design Documentation for Google Ads.pdf"
    with open(file_name, mode="rb") as file:
        # upload_file = UploadFile(filename=file_name, file=file)
        content = file.read()
        await datastore.put_data(ctx, user=user, db=db, file=(file_name, content))
    chat_req = LlmChatOpenaiConfig(
        provider=provider,
        messages=[ChatCompletionUserMessageParam(role="user", content="Write a simple story")],
        model="gpt-4.1-mini",
    )
    action_config = ActionCreateMultiModalLlmChatWithVectorSearchOpenaiConfig(
        datastore_id=datastore.id, operations=Operations.search, chat_req=chat_req
    )
    action_doc = ActionDoc(
        id="action_doc_1",
        user_id=user.id,
        name="action_1",
        type=ActionType.multimodal_llm_chat_with_vector_search_openai,
        config=action_config.model_dump(exclude_none=True),
    )

    action_input = {"text": query}
    app_state = AppState()
    action_run_obj_1 = await ActionFactory.run_action(ctx, app_state, action_doc, action_input, user, db=db)
    # print(action_run_obj_1)
    assert action_run_obj_1
    # On Action run: for every run we add context input
