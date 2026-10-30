import pytest

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_grok import (
    ActionText2TextLlmChatGrok,
    GrokChatConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.user.user_orm_model import UserORM

ActionText2TextLlmChatGrok


def init_loader():
    settings: Settings = SettingsProvider.sget()
    return settings.GROK_API_KEY, settings.GROK_MANAGEMENT_API_KEY


@pytest.mark.asyncio
async def test_grok_chat_basic(ctx, db, set_test_settings):
    api_key, mapi_key = init_loader()
    if not api_key:
        pytest.skip("grok api not configured")

    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    config = GrokChatConfig(
        messages=[{"role": "user", "content": "you are a helpful assistant"}],
        model="grok-4.6",
        max_rounds=2,
    )
    action_doc = ActionDoc(
        id="action_doc_1",
        user_id=user.id,
        name="action_1",
        type=ActionType.text2text_llm_chat_grok,
        config=config.model_dump(exclude_none=True),
    )
    config = ActionText2TextLlmChatGrok.get_config_type().model_validate(action_doc.config)
    action = ActionText2TextLlmChatGrok(
        db=db,
        user=user,
        action_config=GrokChatConfig(
            messages=[{"role": "user", "content": "you are a helpful assistant"}],
            model="grok-4.6",
            max_rounds=2,
        ),
    )

    inp = DataBlock(text="Say hello in one sentence.")
    out = await action.run_action(ctx=ctx, app_state=AppState(), action_input=inp)

    assert out is not None
    assert hasattr(out, "blocks")
    assert out.blocks
    assert isinstance(out.blocks[0].text, str)
    assert out.blocks[0].text.strip()
