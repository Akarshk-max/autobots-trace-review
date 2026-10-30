from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

import pytest

import src.autobots.ingestion.event_result_hydrator as hydrator
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


@dataclass
class FakeSettings:
    AWS_S3_PUBLIC_BUCKET_NAME: str = "public-bucket"
    AWS_S3_BUCKET_NAME: str = "private-bucket"
    AWS_S3_RUNS_FOLDER: str = "runs"
    AWS_S3_ACTION_RUNS_FOLDER: str = "action_runs"


class FakeStorage:
    def __init__(
        self,
        *,
        provider: str,
        container_name: str,
        owned_url_fragment: str,
    ) -> None:
        self.provider = provider
        self.container_name = container_name
        self.owned_url_fragment = owned_url_fragment

    async def get_key_if_owned_url(self, url: str) -> str | None:
        if self.owned_url_fragment not in url:
            return None
        return url.rsplit("/", 1)[-1]


class CapturingUserFiles:
    """Fake UserFiles capturing the top_level_folder used by the hydrator."""

    last_top_level_folder: Optional[str] = None
    payload: Dict[str, Any] | None = None

    def __init__(self, user: UserORM, s3: Any, top_level_folder: str):  # noqa: ANN401
        CapturingUserFiles.last_top_level_folder = top_level_folder

    async def get_bytes(self, ctx: Context, filename: str) -> bytes | None:
        payload: Dict[str, Any]
        if CapturingUserFiles.payload is None:
            payload = {"_id": filename.removesuffix(".json"), "result": {}}
        else:
            payload = CapturingUserFiles.payload
        return json.dumps(payload).encode("utf-8")


@pytest.mark.asyncio
async def test_hydrator_uses_action_runs_folder_for_action_run(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(hydrator, "UserFiles", CapturingUserFiles)
    monkeypatch.setattr(
        hydrator,
        "get_public_storage",
        lambda object_prefix="": FakeStorage(
            provider="s3",
            container_name="public-bucket",
            owned_url_fragment="public-bucket",
        ),
    )
    monkeypatch.setattr(
        hydrator,
        "get_storage",
        lambda object_prefix="": FakeStorage(
            provider="s3",
            container_name="private-bucket",
            owned_url_fragment="private-bucket",
        ),
    )

    # Patch SettingsProvider.sget used inside the module
    monkeypatch.setattr(hydrator.SettingsProvider, "sget", lambda: FakeSettings())

    CapturingUserFiles.payload = None

    doc: Dict[str, Any] = {
        "_id": "x",
        "type": "action_run",
        "file_url": "https://public-bucket.s3.amazonaws.com/action_runs/u1/x.json",
    }

    user = UserORM(id="u1", email="u1@example.com")
    ctx = Context(user_id="u1")

    _ = await hydrator.hydrate_event_result_from_s3(doc, user, ctx)

    assert CapturingUserFiles.last_top_level_folder == "action_runs"


@pytest.mark.asyncio
async def test_hydrator_uses_runs_folder_for_non_action_run(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(hydrator, "UserFiles", CapturingUserFiles)
    monkeypatch.setattr(
        hydrator,
        "get_public_storage",
        lambda object_prefix="": FakeStorage(
            provider="s3",
            container_name="public-bucket",
            owned_url_fragment="public-bucket",
        ),
    )
    monkeypatch.setattr(
        hydrator,
        "get_storage",
        lambda object_prefix="": FakeStorage(
            provider="s3",
            container_name="private-bucket",
            owned_url_fragment="private-bucket",
        ),
    )
    monkeypatch.setattr(hydrator.SettingsProvider, "sget", lambda: FakeSettings())

    CapturingUserFiles.payload = None

    doc: Dict[str, Any] = {
        "_id": "g1",
        "type": "action_graph_run",
        "file_url": "https://public-bucket.s3.amazonaws.com/runs/u1/g1.json",
    }

    user = UserORM(id="u1", email="u1@example.com")
    ctx = Context(user_id="u1")

    _ = await hydrator.hydrate_event_result_from_s3(doc, user, ctx)

    assert CapturingUserFiles.last_top_level_folder == "runs"


@pytest.mark.asyncio
async def test_hydrator_normalizes_id_to__id(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(hydrator, "UserFiles", CapturingUserFiles)
    monkeypatch.setattr(
        hydrator,
        "get_public_storage",
        lambda object_prefix="": FakeStorage(
            provider="s3",
            container_name="public-bucket",
            owned_url_fragment="public-bucket",
        ),
    )
    monkeypatch.setattr(
        hydrator,
        "get_storage",
        lambda object_prefix="": FakeStorage(
            provider="s3",
            container_name="private-bucket",
            owned_url_fragment="private-bucket",
        ),
    )
    monkeypatch.setattr(hydrator.SettingsProvider, "sget", lambda: FakeSettings())

    # Simulate S3 JSON produced by pydantic with "id" but not "_id".
    CapturingUserFiles.payload = {"id": "x", "result": {}}

    doc: Dict[str, Any] = {
        "_id": "x",
        "type": "action_run",
        "file_url": "https://public-bucket.s3.amazonaws.com/action_runs/u1/x.json",
    }

    user = UserORM(id="u1", email="u1@example.com")
    ctx = Context(user_id="u1")

    full_doc = await hydrator.hydrate_event_result_from_s3(doc, user, ctx)

    assert full_doc.get("_id") == "x"
