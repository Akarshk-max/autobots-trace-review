import base64
from types import SimpleNamespace

import pytest

from src.autobots.action.action_type.action_text2video.action_gemini_omni_video_generation import (
    ActionVideoGenerateGeminiOmni,
)
from src.autobots.conn.gemini.gen_video_omni.gemini_omni import OmniVideoClient
from src.autobots.conn.gemini.gen_video_omni.gemini_omni_models import (
    OmniMediaInput,
    OmniVideoException,
    OmniVideoRequest,
    OmniVideoResult,
)
from src.autobots.data_model.context import Context

VIDEO_BYTES = b"fake-mp4-bytes"
IMAGE_BYTES = b"fake-png-bytes"


class FakeFiles:
    def __init__(self):
        self.uploads = []
        self.downloads = []
        self.gets = []

    async def upload(self, *, file, config):
        self.uploads.append((file.read(), config))
        return SimpleNamespace(name="files/input1", uri="https://files/input1", state="ACTIVE")

    async def get(self, *, name):
        self.gets.append(name)
        return SimpleNamespace(name=name, uri=f"https://files/{name.split('/')[-1]}", state="ACTIVE")

    async def download(self, *, file):
        self.downloads.append(file)
        return VIDEO_BYTES


class FakeInteractions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.create_calls = []
        self.get_calls = []

    async def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return self.responses.pop(0)

    async def get(self, *, id):
        self.get_calls.append(id)
        return self.responses.pop(0)


class FakeGeminiClient:
    def __init__(self, responses):
        self.files = FakeFiles()
        self.interactions = FakeInteractions(responses)

    @property
    def aclient(self):
        return self

    async def wait_for_file_active(self, *, name, **_):
        return await self.files.get(name=name)

    async def aclose(self):
        return None


def completed_interaction(*, data=None, uri=None, interaction_id="int-1"):
    return SimpleNamespace(
        id=interaction_id,
        status="completed",
        errors=None,
        output_video=SimpleNamespace(data=data, uri=uri, mime_type="video/mp4"),
    )


def make_client(responses, monkeypatch):
    fake = FakeGeminiClient(responses)
    client = OmniVideoClient("key", gemini_client=fake, poll_interval_seconds=0)

    async def fake_upload(self, **kwargs):
        return ["https://s3/omni.mp4"]

    monkeypatch.setattr(OmniVideoClient, "_upload_to_s3", fake_upload)
    return client, fake


@pytest.mark.asyncio
async def test_text_to_video_uses_uri_delivery_and_downloads(monkeypatch):
    client, fake = make_client([completed_interaction(uri="https://generativelanguage/v1beta/files/out1")], monkeypatch)
    req = OmniVideoRequest(prompt="a marble rolling", generation_config={"resolution": "720p", "duration_seconds": 5})

    result = await client.generate_video(ctx=Context(), user=SimpleNamespace(id="u"), req=req)

    call = fake.interactions.create_calls[0]
    assert call["model"] == "gemini-omni-1.1-flash"
    assert call["input"] == [{"type": "text", "text": "a marble rolling"}]
    assert call["response_format"] == {"type": "video", "delivery": "uri", "resolution": "720p", "duration": "5s"}
    assert "previous_interaction_id" not in call
    assert fake.files.downloads == ["https://generativelanguage/v1beta/files/out1"]
    assert result.interaction_id == "int-1"
    assert result.s3_urls == ["https://s3/omni.mp4"]
    assert result.videos[0].s3_url == "https://s3/omni.mp4"


@pytest.mark.asyncio
async def test_output_uri_with_download_suffix_polls_bare_file_name(monkeypatch):
    uri = "https://generativelanguage.googleapis.com/v1beta/files/abc123xyz:download?alt=media"
    client, fake = make_client([completed_interaction(uri=uri)], monkeypatch)

    await client.generate_video(ctx=Context(), user=SimpleNamespace(id="u"), req=OmniVideoRequest(prompt="p"))

    assert fake.files.gets == ["files/abc123xyz"]
    assert fake.files.downloads == [uri]


def test_file_name_from_uri_rejects_unknown_shape():
    with pytest.raises(OmniVideoException):
        OmniVideoClient._file_name_from_uri("https://example.com/no-file-here")


@pytest.mark.asyncio
async def test_image_to_video_sends_inline_image_and_task(monkeypatch):
    client, fake = make_client([completed_interaction(data=base64.b64encode(VIDEO_BYTES).decode())], monkeypatch)
    image_b64 = base64.b64encode(IMAGE_BYTES).decode()
    req = OmniVideoRequest(
        prompt="animate this",
        image=OmniMediaInput(base64_content=image_b64, mime_type="image/png"),
        generation_config={"task": "image_to_video"},
    )

    result = await client.generate_video(ctx=Context(), user=SimpleNamespace(id="u"), req=req)

    call = fake.interactions.create_calls[0]
    assert call["input"] == [
        {"type": "image", "data": image_b64, "mime_type": "image/png"},
        {"type": "text", "text": "animate this"},
    ]
    assert call["generation_config"] == {"video_config": {"task": "image_to_video"}}
    assert fake.files.downloads == []
    assert result.provider_uris == []


@pytest.mark.asyncio
async def test_video_input_is_uploaded_to_files_api(monkeypatch):
    client, fake = make_client([completed_interaction(uri="https://x/files/out2")], monkeypatch)
    req = OmniVideoRequest(
        prompt="continue the scene",
        video=OmniMediaInput(base64_content=base64.b64encode(VIDEO_BYTES).decode(), mime_type="video/mp4"),
    )

    await client.generate_video(ctx=Context(), user=SimpleNamespace(id="u"), req=req)

    assert fake.files.uploads[0][0] == VIDEO_BYTES
    assert fake.interactions.create_calls[0]["input"][0] == {"type": "video", "uri": "https://files/input1"}


@pytest.mark.asyncio
async def test_previous_interaction_id_is_forwarded(monkeypatch):
    client, fake = make_client([completed_interaction(uri="https://x/files/out3", interaction_id="int-2")], monkeypatch)
    req = OmniVideoRequest(prompt="make the violin invisible", previous_interaction_id="int-1")

    result = await client.generate_video(ctx=Context(), user=SimpleNamespace(id="u"), req=req)

    assert fake.interactions.create_calls[0]["previous_interaction_id"] == "int-1"
    assert result.interaction_id == "int-2"


@pytest.mark.asyncio
async def test_pending_interaction_is_polled_until_completed(monkeypatch):
    pending = SimpleNamespace(id="int-3", status="in_progress", errors=None, output_video=None)
    client, fake = make_client([pending, completed_interaction(uri="https://x/files/out4", interaction_id="int-3")], monkeypatch)

    result = await client.generate_video(ctx=Context(), user=SimpleNamespace(id="u"), req=OmniVideoRequest(prompt="p"))

    assert fake.interactions.get_calls == ["int-3"]
    assert result.interaction_id == "int-3"


@pytest.mark.asyncio
async def test_failed_interaction_raises(monkeypatch):
    failed = SimpleNamespace(id="int-4", status="failed", errors=[SimpleNamespace(message="unsafe prompt")], output_video=None)
    client, _ = make_client([failed], monkeypatch)

    with pytest.raises(OmniVideoException) as exc:
        await client.generate_video(ctx=Context(), user=SimpleNamespace(id="u"), req=OmniVideoRequest(prompt="p"))

    assert exc.value.payload.code == "OMNI_INTERACTION_NOT_COMPLETED"
    assert "unsafe prompt" in exc.value.payload.message


def test_media_input_requires_exactly_one_source():
    with pytest.raises(ValueError):
        OmniMediaInput(url="https://a", file_path="/b")
    with pytest.raises(ValueError):
        OmniMediaInput()


def test_action_infers_media_from_urls_and_builds_output():
    image, video = ActionVideoGenerateGeminiOmni._infer_media_from_urls(
        ["https://cdn/a.png?x=1", "https://cdn/b.mp4", "https://cdn/c.jpg"]
    )
    assert image.url == "https://cdn/a.png?x=1"
    assert video.url == "https://cdn/b.mp4"

    out = ActionVideoGenerateGeminiOmni._to_output(
        OmniVideoResult(model="m", interaction_id="int-9", s3_urls=["https://s3/v.mp4"], provider_uris=["https://p/v"])
    )
    assert out.blocks[0].urls == ["https://s3/v.mp4"]
    assert out.blocks[1].text == "interaction_id: int-9"
    assert out.blocks[2].text == "provider_uri: https://p/v"
