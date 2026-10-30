import pytest
from openai.types.chat import ChatCompletionUserMessageParam

from src.autobots.action.action.action_crud import ActionCRUD
from src.autobots.action.action.action_doc_model import ActionDocCreate, ActionDocFind
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_factory import ActionFactory, RunActionObj
from src.autobots.action.action_type.action_map import ACTION_MAP
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    LlmChatOpenaiConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.openai.openai_chat.chat_model import ChatReq
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.data_model.app_state import AppState


@pytest.mark.asyncio
async def test_action_crud_happy_path(ctx, db, user, set_test_settings):
    action_crud = ActionCRUD(db)

    try:
        chat_req = LlmChatOpenaiConfig(
            provider="azure",
            model="o3-mini",
            messages=[
                ChatCompletionUserMessageParam(role="user", content="You are an expert blogger")
            ],
        )
        action_doc_create = ActionDocCreate(
            name="test_action_crud_happy_path",
            type=ActionType.text2text_llm_chat_openai,
            config=chat_req.model_dump(),
            # input=None,
            # output=None,
            user_id=user.id,
        )
        inserted = await action_crud.insert_one(action_doc_create)
        assert inserted is not None

        action_find = ActionDocFind(user_id=user.id, id=str(inserted.id))
        action_doc = await action_crud.find_one(action_find)
        # assert len(action_docs) == 1
        # action_doc = action_docs.pop()

        assert action_doc.type == ActionType.text2text_llm_chat_openai
        user_input = DataBlock(text="Blog on San Francisco")
        action_factory = ActionFactory()
        # resp: TextObjs = await action_factory.run_action(action_doc, user_input.model_dump())
        # assert len(resp.texts) > 0
        # assert resp.texts[0] != ""
        app_state = AppState()
        run_action_obj: RunActionObj = await action_factory.run_action(
            ctx,app_state, action_doc, user_input.model_dump(), user, db=db
        )
        resp = (
            ACTION_MAP.get(ActionType.text2text_llm_chat_openai)
            .get_output_type()
            .model_validate(run_action_obj.output_dict)
        )
        assert len(resp.blocks) > 0
        assert resp.blocks[0].text != ""

        delete_result = await action_crud.delete_many(action_find)
        assert delete_result.deleted_count == 1
    except Exception as e:
        assert e is None
    finally:
        find = ActionDocFind(user_id=user.id, name="test_action_crud_happy_path")
        deleted = await action_crud.delete_many(find)
        assert deleted.deleted_count == 0


@pytest.mark.asyncio
async def test_action_search_happy_path(ctx, db, user, set_test_settings):
    action_crud = ActionCRUD(db)
    try:
        chat_req = ChatReq(
            model="o3-mini",
            messages=[
                ChatCompletionUserMessageParam(role="user", content="You are an expert blogger")
            ],
        )
        action_doc_create = ActionDocCreate(
            name="test action crud happy path",
            type=ActionType.text2text_llm_chat_openai,
            config=chat_req.model_dump(),
            # input=None,
            # output=None,
            user_id=user.id,
        )
        inserted = await action_crud.insert_one(action_doc_create)
        assert inserted is not None

        search_doc = CollectionDocSearch(query="action crud happy", user_id=user.id)

        searched_docs = await action_crud.search(search_doc=search_doc)
        assert len(searched_docs) > 0

        action_find = ActionDocFind(user_id=user.id, id=str(inserted.id))
        delete_result = await action_crud.delete_many(action_find)
        assert delete_result.deleted_count == 1
    except Exception as e:
        assert e is None
    finally:
        find = ActionDocFind(user_id=str(user.id), name="test_action_crud_happy_path")
        deleted = await action_crud.delete_many(find)
        assert deleted.deleted_count == 0
