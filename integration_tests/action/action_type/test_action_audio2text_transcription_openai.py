from typing import Literal

import pytest

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_audio2text.action_audio2text_transcription_openai import (
    ActionAudio2TextTranscriptionOpenai,
    TranscriptionOpenaiConfig,
)
from src.autobots.data_model.app_state import AppState


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_action_audio2text_transcription_openai_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    config = TranscriptionOpenaiConfig(provider=provider)
    input = DataBlock(urls=["https://cdn.openai.com/API/docs/audio/alloy.wav"])
    action = ActionAudio2TextTranscriptionOpenai(action_config=config, user=user, db=db)

    app_state = AppState()
    data_blocks = await action.run_action(ctx, app_state, input)
    assert len(data_blocks.blocks) > 0
    data_block = data_blocks.blocks.pop()
    assert data_block.text != ""
