from pathlib import Path
from typing import Literal

import pytest
from openai.types import ImagesResponse

from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.openai.openai_images.image_model import (
    ImageCreateVariation,
    ImageEdit,
    ImageReq,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_create_image(ctx, db, user, provider: Literal["azure", "openai"], set_test_settings):
    params = ImageReq(prompt="A goal by Arsenal against Manchester city")
    client = await AppConn.get_openai(ctx=ctx, user=user, db=db, provider=provider)
    resp: ImagesResponse | Exception = await client.openai_images.create_image(params)
    assert len(resp.data) > 0
    assert resp.data[0].url


@pytest.mark.skip("Not supported yet in Azure OpenAi")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider",
    [
        # "azure",
        "openai"
    ],
)
async def test_create_image_edit_file_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    SOURCE_FILE = "tests/resources/openai/test_images/suit_1.png"
    path = Path(SOURCE_FILE)

    params = ImageEdit(image=path, prompt="smiling face")
    client = await AppConn.get_openai(ctx=ctx, user=user, db=db, provider=provider)
    resp: ImagesResponse | None = await client.openai_images.create_image_edit(params)
    assert resp is not None


@pytest.mark.skip("Not supported yet in Azure OpenAi")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider",
    [
        # "azure", # "Not supported yet in Azure OpenAi"
        "openai"
    ],
)
async def test_create_image_variation_file_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    SOURCE_FILE = "tests/resources/openai/test_images/suit_1.png"

    params = ImageCreateVariation(image=SOURCE_FILE)
    client = await AppConn.get_openai(ctx=ctx, user=user, db=db, provider=provider)
    resp: ImagesResponse | None = await client.openai_images.create_image_variation(params)
    assert resp is not None
