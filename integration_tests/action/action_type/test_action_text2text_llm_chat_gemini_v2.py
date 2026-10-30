import pytest
from google.genai import types

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_gemini import (
    ActionText2TextLlmChatGemini,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.gemini.gemini_model import ChatReqGemini

# @pytest.mark.asyncio
# async def test_gemini_conversation_happy_path_1(set_test_settings):
#     genai.configure(api_key=SettingsProvider.sget().GEMINI_API_KEY)
#     model = genai.GenerativeModel("gemini-1.5-flash")
#     response = model.generate_content("Write a story about a magic backpack.")
#     print(response.text)


@pytest.mark.asyncio
async def test_gemini_conversation_happy_path_2(ctx, db, user, set_test_settings):
    chat_req = ChatReqGemini(
        contents=[
            types.Content(
                role="user", parts=[types.Part(text="Write a story about a magic backpack.")]
            )
        ]
    )
    action_doc = ActionDoc(
        id="action_doc_1",
        user_id=user.id,
        name="action_1",
        type=ActionType.text2text_llm_chat_gemini,
        config=chat_req.model_dump(exclude_none=True),
    )
    config = ActionText2TextLlmChatGemini.get_config_type().model_validate(action_doc.config)
    client = await AppConn.get_gemini(ctx=ctx, db=db, user=user)
    response: types.GenerateContentResponse = await client.generate(
        ctx=ctx,
        user=user,
        chat_req=config,
        db=db,
    )

    assert response
    assert len(response.parts) > 0
