import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.autobots.ingestion import event_result_hydrator as hydrator

pytestmark = pytest.mark.asyncio


class FakeSettings:
    AWS_S3_RUNS_FOLDER = "runs"
    AWS_S3_ACTION_RUNS_FOLDER = "action_runs"


def make_storage(
    *,
    provider: str,
    container: str,
    owned_key: str | None,
) -> MagicMock:
    storage = MagicMock()
    storage.provider = provider
    storage.container_name = container
    storage.get_key_if_owned_url = AsyncMock(return_value=owned_key)
    return storage


async def test_hydrator_routes_azure_public_url_to_public_storage(
    monkeypatch,
):
    public = make_storage(
        provider="azure",
        container="public-container",
        owned_key="runs/u1/doc-1.json",
    )
    private = make_storage(
        provider="azure",
        container="private-container",
        owned_key=None,
    )

    captured: dict[str, object] = {}

    class FakeUserFiles:
        def __init__(self, user, s3, top_level_folder):
            captured["storage"] = s3
            captured["folder"] = top_level_folder

        async def get_bytes(self, ctx, filename):
            return json.dumps({"_id": "doc-1", "result": {"ok": True}}).encode()

    monkeypatch.setattr(
        hydrator,
        "get_public_storage",
        lambda object_prefix="": public,
    )
    monkeypatch.setattr(
        hydrator,
        "get_storage",
        lambda object_prefix="": private,
    )
    monkeypatch.setattr(hydrator, "UserFiles", FakeUserFiles)
    monkeypatch.setattr(
        hydrator.SettingsProvider,
        "sget",
        lambda: FakeSettings(),
    )

    doc = {
        "_id": "doc-1",
        "type": "action_graph_run",
        "file_url": ("https://account.blob.core.windows.net/public-container/runs/u1/doc-1.json"),
    }

    result = await hydrator.hydrate_event_result_from_s3(
        doc,
        MagicMock(),
        MagicMock(),
    )

    assert result["result"] == {"ok": True}
    assert captured["storage"] is public
    assert captured["folder"] == "runs"
    public.get_key_if_owned_url.assert_awaited_once_with(doc["file_url"])
    private.get_key_if_owned_url.assert_not_awaited()


async def test_hydrator_routes_private_url_after_public_rejects_it(
    monkeypatch,
):
    public = make_storage(
        provider="azure",
        container="public-container",
        owned_key=None,
    )
    private = make_storage(
        provider="azure",
        container="private-container",
        owned_key="action_runs/u1/doc-2.json",
    )

    captured: dict[str, object] = {}

    class FakeUserFiles:
        def __init__(self, user, s3, top_level_folder):
            captured["storage"] = s3
            captured["folder"] = top_level_folder

        async def get_bytes(self, ctx, filename):
            return json.dumps({"_id": "doc-2"}).encode()

    monkeypatch.setattr(
        hydrator,
        "get_public_storage",
        lambda object_prefix="": public,
    )
    monkeypatch.setattr(
        hydrator,
        "get_storage",
        lambda object_prefix="": private,
    )
    monkeypatch.setattr(hydrator, "UserFiles", FakeUserFiles)
    monkeypatch.setattr(
        hydrator.SettingsProvider,
        "sget",
        lambda: FakeSettings(),
    )

    doc = {
        "_id": "doc-2",
        "type": "action_run",
        "file_url": (
            "https://account.blob.core.windows.net/private-container/action_runs/u1/doc-2.json"
        ),
    }

    result = await hydrator.hydrate_event_result_from_s3(
        doc,
        MagicMock(),
        MagicMock(),
    )

    assert result["_id"] == "doc-2"
    assert captured["storage"] is private
    assert captured["folder"] == "action_runs"
    public.get_key_if_owned_url.assert_awaited_once_with(doc["file_url"])
    private.get_key_if_owned_url.assert_awaited_once_with(doc["file_url"])


async def test_hydrator_rejects_url_not_owned_by_system_storage(
    monkeypatch,
):
    public = make_storage(
        provider="azure",
        container="public-container",
        owned_key=None,
    )
    private = make_storage(
        provider="azure",
        container="private-container",
        owned_key=None,
    )

    user_files = MagicMock()

    monkeypatch.setattr(
        hydrator,
        "get_public_storage",
        lambda object_prefix="": public,
    )
    monkeypatch.setattr(
        hydrator,
        "get_storage",
        lambda object_prefix="": private,
    )
    monkeypatch.setattr(hydrator, "UserFiles", user_files)
    monkeypatch.setattr(
        hydrator.SettingsProvider,
        "sget",
        lambda: FakeSettings(),
    )

    doc = {
        "_id": "doc-3",
        "type": "action_run",
        "file_url": "https://foreign.example/doc-3.json",
        "status": "lite",
    }

    result = await hydrator.hydrate_event_result_from_s3(
        doc,
        MagicMock(),
        MagicMock(),
    )

    assert result is doc
    user_files.assert_not_called()
