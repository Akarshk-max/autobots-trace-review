import os

import pytest

from src.autobots.conn.grok.grok import get_grok_conn
from src.autobots.conn.grok.grok_image.grok_image import XAIImageManager
from src.autobots.conn.grok.grok_image.image_models import XAIImageFetchError, XAIImageSaveError
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.user.user_orm_model import UserORM

_SAMPLE_IMAGE_URL = os.getenv(
    "GROK_SAMPLE_IMAGE_URL",
    "https://picsum.photos/300",
)


def init_loader():
    settings: Settings = SettingsProvider.sget()
    return settings.GROK_API_KEY, settings.GROK_MANAGEMENT_API_KEY


@pytest.mark.asyncio
async def test_grok_image_generate_basic(ctx, db, set_test_settings):
    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")

    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client

    mgr = XAIImageManager()
    prompt = "A small cat sitting on a chair, digital art"

    out = await mgr.generate(
        ctx=ctx,
        app_state=AppState(),
        user=user,
        db=db,
        client=client,
        prompt=prompt,
        n=1,
        include_image_bytes=True,
        fetch_bytes_if_url=True,
    )

    assert out.images
    img = out.images[0]
    assert img.provider_url or img.image_bytes is not None
    assert out.request.prompt == prompt


@pytest.mark.asyncio
async def test_grok_image_generate_multiple_n(ctx, db, set_test_settings):
    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")

    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client

    mgr = XAIImageManager()

    prompt = "A colorful butterfly with neon wings"
    out = await mgr.generate(
        ctx=ctx, app_state=AppState(), user=user, db=db, client=client, prompt=prompt, n=2
    )

    assert len(out.images) == 2
    assert all(img.index in (0, 1) for img in out.images)


@pytest.mark.asyncio
async def test_grok_image_generate_urls_only_no_bytes(ctx, db, set_test_settings):
    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")

    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client

    mgr = XAIImageManager()
    out = await mgr.generate(
        ctx=ctx,
        app_state=AppState(),
        user=user,
        db=db,
        client=client,
        prompt="A forest in studio-ghibli style",
        urls_only=True,
    )

    assert out.images
    img = out.images[0]
    assert img.url or img.provider_url
    assert img.image_bytes is None


@pytest.mark.asyncio
async def test_grok_image_generate_many_prompts(ctx, db, set_test_settings):
    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")

    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client

    mgr = XAIImageManager()
    prompts = ["A red dragon flying", "A blue robot skating"]

    res = await mgr.generate_many_prompts(
        ctx=ctx,
        user=user,
        db=db,
        client=client,
        prompts=prompts,
        concurrency=2,
        n_per_prompt=1,
    )

    assert len(res) == 2
    for p, result in res.items():
        assert result.images
        assert p in res


@pytest.mark.asyncio
async def test_grok_image_fetch_invalid_url_raises(ctx, db, set_test_settings):
    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")

    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client
    assert client

    mgr = XAIImageManager()

    with pytest.raises(XAIImageFetchError):
        await mgr._fetch_https_bytes(
            ctx=ctx,
            url="http://non-secure.com/img.png",
            timeout_sec=5,
            on_progress=None,
            max_bytes=None,
        )


@pytest.mark.asyncio
async def test_grok_image_s3_upload(ctx, db, set_test_settings):
    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")

    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client

    mgr = XAIImageManager()
    out = await mgr.generate(
        ctx=ctx,
        app_state=AppState(),
        user=user,
        db=db,
        client=client,
        prompt="A futuristic city skyline at sunset",
        save_to_s3=True,
    )

    img = out.images[0]
    assert img.url is not None
    assert img.url.startswith("https://") or img.url.startswith("http")


@pytest.mark.asyncio
async def test_grok_image_error_on_upload_without_bytes(ctx, db, set_test_settings):
    """Saving to S3 should fail if user requests url-only images with no bytes fetched."""

    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")

    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client

    mgr = XAIImageManager()

    with pytest.raises(XAIImageSaveError):
        await mgr.generate(
            ctx=ctx,
            app_state=AppState(),
            user=user,
            db=db,
            client=client,
            prompt="Ocean waves like oil painting",
            save_to_s3=True,
            urls_only=True,  # no bytes - upload must fail
        )
