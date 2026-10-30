import pytest
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.conn.openai.openai_chat.chat_model import ChatReq
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.asyncio
@pytest.mark.skip("use it to debug")
async def test_action_text2text_llm_chat_openai_rerun_happy_path_12345():
    input = {"text": "What is ai"}
    db_gen = get_mongo_db()
    db: AsyncIOMotorDatabase = next(db_gen)
    # action_run_obj_1 = await ActionFactory.run_action(Context(), action_doc, action_input)
    user_orm = UserORM(id="user1")  # here
    user_actions = UserActions(user_orm, db)
    action_doc = await user_actions.get_action("")  # here
    user_action_result = UserActionResult(user_orm, db)
    action_result_doc = await ActionFactory().run_action_in_background(
        ctx=Context(),
        app_state= AppState(),
        action_doc=action_doc,
        action_input_dict=input,
        user_action_result=user_action_result,
        db=db,
        action_result_id="",  # here
    )
    assert action_result_doc


# asyncio.run(test_action_text2text_llm_chat_openai_rerun_happy_path())


@pytest.mark.asyncio
async def test_chat_req():
    chat_req_dict = {
        "messages": [
            {"content": "test ", "role": "system"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://picsum.photos/id/20/200/300"},
                    }
                ],
            },
            {"role": "user", "content": "explain"},
            {
                "content": "This image depicts a workspace setup with various items arranged on a desk. Here's a detailed breakdown of the elements present in the image:\n\n1. **Laptop:** In the upper left corner, there is an open laptop, suggesting that this workspace may be used for tasks such as studying, working, or browsing.\n\n2. **Books:** There are several books lined up vertically at the top of the image. They are likely reference or study materials.\n\n3. **Notebook:** On the left side of the image, there is a spiral-bound notebook, which could be used for jotting down notes or ideas.\n\n4. **Pencils:** Three colored pencils are placed on the left side of the open book.\n\n5. **Open Book or Magazine:** In the center of the image, there is an open book or magazine displaying various colorful logos or graphics. It could be a design book or a catalog.\n\n6. **Smartphone:** Placed on top of the open book, there is a smartphone displaying its home screen with app icons. This indicates that the person using this workspace might be engaged in multitasking or using their phone for additional resources or communication.\n\nThe overall arrangement of these items suggests a well-organized and productive workspace environment, possibly aimed at creative or academic work.",
                "role": "assistant",
            },
        ],
        "model": "gpt-4o",
        "frequency_penalty": 0.0,
        "max_tokens": 2000,
        "n": 1,
        "presence_penalty": 0.0,
        "stop": "",
        "stream": False,
        "temperature": 0.8,
        "top_p": 1.0,
        "user": "",
        "timeout": 180.0,
    }

    chat_req = ChatReq.model_validate(chat_req_dict)
    new_chat_dict = chat_req.model_dump()
    assert new_chat_dict
    assert chat_req.messages
