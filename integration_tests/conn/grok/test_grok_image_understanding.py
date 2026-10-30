import pytest
from loguru import logger

from src.autobots.conn.grok.grok import get_grok_conn
from src.autobots.conn.grok.grok_chat.grok_chat import GrokChat
from src.autobots.conn.grok.grok_chat.grok_models import GrokChatRequest, GrokMessage
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.user.user_orm_model import UserORM


def init_loader():
    settings: Settings = SettingsProvider.sget()
    return settings.GROK_API_KEY, settings.GROK_MANAGEMENT_API_KEY


@pytest.mark.asyncio
async def test_grok_chat(ctx, db, set_test_settings):
    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client

    grok_chat = GrokChat(client=client, timeout_sec=500.0)
    # tool_specs = build_custom_tools_from_tools_map([ "sql_runner" , "pdf_parser" ])
    req = GrokChatRequest(
        messages=[
            GrokMessage(role="system", content="You are a helpful assistant."),
            GrokMessage(role="user", content="Explain the given image."),
        ],
        # custom_tools=tool_specs,
        image_urls=["https://ai.google.dev/static/gemini-api/docs/images/infographic-spanish.png"],
    )

    out = await grok_chat.run(
        ctx=ctx, app_state=AppState(), user=user, db=db, max_rounds=5, req=req
    )
    assert out.content
    logger.bind(ctx=ctx, user=user).info(f"output is {out}")
