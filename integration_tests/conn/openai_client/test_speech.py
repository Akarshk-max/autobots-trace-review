import io
from typing import Literal

import pytest
from openai._legacy_response import HttpxBinaryResponseContent

from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.openai.openai_audio.speech_model import SpeechReq
from src.autobots.core.clock.clock import Clock
from src.autobots.core.utils import gen_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_speech_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    input = f"Hello! I am Autobot X. I am here to assist you. Today is {Clock.datetime_now().strftime('%B %d, %Y')}."
    params = SpeechReq(input=input)
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=provider)
    resp: HttpxBinaryResponseContent = await client.openai_audio.speech(speech_req=params)
    assert resp is not None

    url = await get_public_s3().put_file_obj(
        io.BytesIO(resp.content), f"{gen_hash(input)}.{params.response_format}"
    )
    assert url != ""

    # resp.stream_to_file(f"./tests/resources/test.{params.response_format}")
