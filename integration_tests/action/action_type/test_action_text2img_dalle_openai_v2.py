from typing import Literal

import pytest

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_text2img.action_text2img_dalle_openai_v2 import (
    ActionGenImageDalleOpenAiV2,
    DalleOpenaiConfig,
)
from src.autobots.data_model.app_state import AppState


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_action_text2img_dalle_openai_v2_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    app_state = AppState()
    config = DalleOpenaiConfig(
        provider=provider,
        prompt="A goal by Arsenal against Manchester city",
    )
    res = await ActionGenImageDalleOpenAiV2(action_config=config, db=db, user=user).run_action(
        ctx=ctx, app_state=app_state, action_input=DataBlock()
    )
    assert res and len(res.blocks[0].urls) > 0
