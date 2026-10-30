import pytest
from openai.types.chat import ChatCompletionUserMessageParam

from src.autobots.action.action.action_crud import ActionCRUD
from src.autobots.action.action.action_doc_model import (
    ActionDocCreate,
    ActionDocFind,
    ActionUpdate,
)
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    LlmChatOpenaiConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.asyncio
async def test_share_action_then_get_lite_by_name(ctx, db, user, set_test_settings):
    """
    1. User A creates an action
    2. User A shares the action with User B (by updating shared_with with User B's email)
    3. User B calls get_lite_action_by_name and should find the shared action
    """
    action_crud = ActionCRUD(db)
    action_name = "test_shared_action_get_lite_by_name"

    # User B setup
    user_b = UserORM(id="test_user_b_id", email="user_b@runkai.ai")
    ctx_b = Context(user_id=user_b.id)

    try:
        # Step 1: User A creates an action
        chat_config = LlmChatOpenaiConfig(
            provider="openai",
            model="o3-mini",
            messages=[
                ChatCompletionUserMessageParam(role="user", content="You are a test assistant")
            ],
        )
        action_doc_create = ActionDocCreate(
            name=action_name,
            type=ActionType.text2text_llm_chat_openai,
            config=chat_config.model_dump(),
            user_id=user.id,
        )
        created_action = await action_crud.insert_one(action_doc_create)
        assert created_action is not None

        # Step 2: User A shares the action with User B
        user_a_actions = UserActions(user=user, db=db)
        action_update = ActionUpdate(shared_with=[user_b.email])
        updated_action = await user_a_actions.update_action(
            ctx, created_action.id, action_update
        )
        assert updated_action is not None
        assert user_b.email in updated_action.shared_with

        # Step 3: User B should NOT own the action
        user_b_actions = UserActions(user=user_b, db=db)
        user_b_find = ActionDocFind(user_id=user_b.id, name=action_name)
        user_b_owned = await action_crud.find(user_b_find)
        assert len(user_b_owned) == 0, "User B should not own the action"

        # Step 4: User B calls get_lite_action_by_name and finds the shared action
        lite_doc = await user_b_actions.get_lite_action_by_name(ctx_b, action_name)
        assert lite_doc is not None, "User B should find the shared action by name"
        assert lite_doc.name == action_name
        assert lite_doc.user_id == user.id  # owned by User A
        assert lite_doc.type == ActionType.text2text_llm_chat_openai

    finally:
        # Cleanup: delete the action created by User A
        cleanup_find = ActionDocFind(user_id=user.id, name=action_name)
        await action_crud.delete_many(cleanup_find)
