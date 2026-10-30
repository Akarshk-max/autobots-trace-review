from typing import Literal

import pytest
from pydantic import HttpUrl

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_text2audio.action_text2audio_speech_openai import (
    ActionText2AudioSpeechOpenai,
    SpeechOpenaiConfig,
)
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.app_state import AppState


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_action_text2audio_speech_openai_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    config = SpeechOpenaiConfig(
        provider=provider,
        input="Hello! I am Autobot X. I am here to assist you.",
    )
    input = DataBlock(text=f"Today is {Clock.datetime_now().strftime('%B %d, %Y')}.")
    app_state = AppState()
    audio_res = await ActionText2AudioSpeechOpenai(
        action_config=config, db=db, user=user
    ).run_action(ctx, app_state, input)
    assert audio_res and len(audio_res.blocks[0].urls) > 0

    for url in audio_res.blocks[0].urls:
        await get_public_s3().delete_https_url(url=HttpUrl(url))
