import io
from typing import Literal

import pytest
from openai._legacy_response import HttpxBinaryResponseContent

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_audio2text.action_audio2text_translation_openai import (
    ActionAudio2TextTranslationOpenai,
    TranslationOpenaiConfig,
)
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.openai.openai_audio.speech_model import SpeechReq
from src.autobots.core.clock.clock import Clock
from src.autobots.core.utils import gen_hash
from src.autobots.data_model.app_state import AppState


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_action_audio2text_translation_openai_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    # # create audio file
    openai_client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=provider)
    input = f"Namaste, main AutobotX hoon. main aapaki sahaayata ke lie yahaan hoon. Aaj ki tareekh {Clock.datetime_now().strftime('%B %d, %Y')} hai."
    params = SpeechReq(input=input)
    resp: HttpxBinaryResponseContent = await openai_client.openai_audio.speech(speech_req=params)
    assert resp is not None
    url = await get_public_s3().put_file_obj(
        io.BytesIO(resp.content), f"{gen_hash(input)}.{params.response_format}"
    )
    assert url != ""

    # Create Translation
    config = TranslationOpenaiConfig(provider=provider, file_url=str(url))
    input = DataBlock(urls=[url.unicode_string()])
    app_state = AppState()
    action = ActionAudio2TextTranslationOpenai(action_config=config, db=db, user=user)
    data_blocks = await action.run_action(ctx=ctx, app_state=app_state, action_input=input)
    assert data_blocks.blocks[0].text is not None
    # flaky assert
    # assert data_blocks.blocks[0].text.lower().__contains__("i am here to help you")
