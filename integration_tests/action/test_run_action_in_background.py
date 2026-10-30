import pytest
from openai.types.chat import ChatCompletionUserMessageParam

from src.autobots.action.action.action_crud import ActionCRUD
from src.autobots.action.action.action_doc_model import ActionDocCreate, ActionDocFind
from src.autobots.action.action.common_action_models import TextObj
from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.openai.openai_chat.chat_model import ChatReq
from src.autobots.data_model.app_state import AppState


@pytest.mark.asyncio
async def test_background_run_of_action_mock_happy_path(ctx, db, user, set_test_settings):
    action_crud = ActionCRUD(db)
    user_action_result = UserActionResult(user=user, db=db)
    action_result_doc = None
    action_name = "test_action_mock_happy_path"

    try:
        action_doc_create = ActionDocCreate(
            name=action_name,
            type=ActionType.mock_action,
            config=TextObj(text="mock_action").model_dump(),
            # input=None,
            # output=None,
            user_id=user.id,
        )
        inserted = await action_crud.insert_one(action_doc_create)
        assert inserted is not None

        action_find = ActionDocFind(id=str(inserted.id), user_id=user.id)
        action_doc = await action_crud.find_one(action_find)
        # assert len(action_docs) == 1
        # action_doc = action_docs.pop()

        action_input = TextObj(text="okok")
        app_state = AppState()
        action_result_doc = await ActionFactory().run_action_in_background(
            ctx, app_state, action_doc, action_input.model_dump(), user_action_result, db=db
        )

    except Exception as e:
        assert e is None
    finally:
        deleted = await user_action_result.delete_action_result(action_result_doc.id)
        assert deleted == 1

        find = ActionDocFind(name=action_name, user_id=user.id)
        deleted = await action_crud.delete_many(find)
        assert deleted.deleted_count == 1


@pytest.mark.asyncio
async def test_background_run_of_action_llm_chat_happy_path(ctx, db, user, set_test_settings):
    action_crud = ActionCRUD(db)
    user_action_result = UserActionResult(user, db)
    action_result_doc = None
    action_name = "test_action_llm_chat_happy_path"

    try:
        chat_req = ChatReq(
            messages=[
                ChatCompletionUserMessageParam(role="user", content="You are an expert blogger")
            ]
        )
        action_doc_create = ActionDocCreate(
            name=action_name,
            type=ActionType.text2text_llm_chat_openai,
            config=chat_req.model_dump(),
            # input=None,
            # output=None,
            user_id=user.id,
        )
        inserted = await action_crud.insert_one(action_doc_create)
        assert inserted is not None

        action_find = ActionDocFind(id=str(inserted.id), user_id=user.id)
        action_doc = await action_crud.find_one(action_find)
        # assert len(action_docs) == 1
        # action_doc = action_docs.pop()

        action_input = TextObj(text="okok")
        app_state = AppState()
        action_result_doc = await ActionFactory().run_action_in_background(
            ctx, app_state, action_doc, action_input.model_dump(), user_action_result, db=db
        )

    except Exception as e:
        assert e is None
    finally:
        deleted = await user_action_result.delete_action_result(action_result_doc.id)
        assert deleted == 1

        find = ActionDocFind(name=action_name, user_id=user.id)
        deleted = await action_crud.delete_many(find)
        assert deleted.deleted_count == 1
