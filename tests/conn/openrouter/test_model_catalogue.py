import asyncio
import json

import pytest

from src.autobots.conn.openrouter import model_capabilities
from src.autobots.conn.openrouter.model_capabilities import ModelCatalogue

UNREACHABLE_URL = "http://127.0.0.1:1/models"


def _write_fallback(path, models):
    payload = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "total_models": len(models),
        "source": "test",
        "models": {
            model_id: {
                "id": model_id,
                "name": model_id,
                "capabilities": capabilities,
            }
            for model_id, capabilities in models.items()
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _api_payload(models):
    return {
        "data": [
            {
                "id": model_id,
                "name": model_id,
                "supported_parameters": ["tools"] if tool_calling else [],
                "architecture": {"input_modalities": ["text"]},
            }
            for model_id, tool_calling in models.items()
        ]
    }


def _catalogue(tmp_path, models_url=UNREACHABLE_URL, fallback=None, **kwargs):
    path = _write_fallback(tmp_path / "models.json", fallback or {})
    return ModelCatalogue(
        models_url=models_url,
        fallback_path=path,
        timeout_seconds=1,
        **kwargs,
    )


class TestFallbackFile:
    @pytest.mark.asyncio
    async def test_a_model_is_served_from_the_file_without_network(self, tmp_path):
        catalogue = _catalogue(tmp_path, fallback={"vendor/a": {"tool_calling": True}})

        model = await catalogue.find("vendor/a")

        assert model is not None
        assert model.capabilities.tool_calling is True

    @pytest.mark.asyncio
    async def test_a_missing_file_leaves_an_empty_catalogue_rather_than_raising(self, tmp_path):
        catalogue = ModelCatalogue(
            models_url=UNREACHABLE_URL,
            fallback_path=tmp_path / "absent.json",
            timeout_seconds=1,
        )

        assert await catalogue.find("vendor/a") is None
        assert catalogue.model_ids() == []

    @pytest.mark.asyncio
    async def test_a_malformed_entry_is_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "models.json"
        path.write_text(
            json.dumps(
                {
                    "models": {
                        "vendor/broken": {"capabilities": "not-a-dict"},
                        "vendor/good": {"id": "vendor/good", "capabilities": {"vision": True}},
                    }
                }
            ),
            encoding="utf-8",
        )
        catalogue = ModelCatalogue(
            models_url=UNREACHABLE_URL, fallback_path=path, timeout_seconds=1
        )

        assert await catalogue.find("vendor/broken") is None
        good = await catalogue.find("vendor/good")
        assert good is not None and good.capabilities.vision is True


class TestUnknownModel:
    @pytest.mark.asyncio
    async def test_an_unknown_model_returns_none_rather_than_raising(self, tmp_path):
        catalogue = _catalogue(tmp_path, fallback={"vendor/a": {}})

        assert await catalogue.find("vendor/unknown") is None


class TestRefresh:
    @pytest.mark.asyncio
    async def test_a_successful_refresh_replaces_the_catalogue(self, tmp_path, monkeypatch):
        catalogue = _catalogue(tmp_path, fallback={"vendor/old": {}})
        _patch_fetch(monkeypatch, _api_payload({"vendor/new": True}))

        assert await catalogue.refresh() is True

        assert await catalogue.find("vendor/new") is not None
        assert catalogue.model_ids() == ["vendor/new"]

    @pytest.mark.asyncio
    async def test_a_failed_refresh_keeps_serving_the_existing_catalogue(self, tmp_path):
        catalogue = _catalogue(tmp_path, fallback={"vendor/a": {"tool_calling": True}})
        await catalogue.find("vendor/a")

        assert await catalogue.refresh() is False

        model = await catalogue.find("vendor/a")
        assert model is not None and model.capabilities.tool_calling is True

    @pytest.mark.asyncio
    async def test_an_empty_response_does_not_wipe_the_catalogue(self, tmp_path, monkeypatch):
        catalogue = _catalogue(tmp_path, fallback={"vendor/a": {}})
        await catalogue.find("vendor/a")
        _patch_fetch(monkeypatch, {"data": []})

        assert await catalogue.refresh() is False

        assert await catalogue.find("vendor/a") is not None

    @pytest.mark.asyncio
    async def test_concurrent_refreshes_hit_the_api_once(self, tmp_path, monkeypatch):
        catalogue = _catalogue(tmp_path, fallback={"vendor/a": {}})
        calls = _patch_fetch(monkeypatch, _api_payload({"vendor/a": True}))

        await asyncio.gather(*(catalogue.refresh() for _ in range(5)))

        assert calls["count"] == 1


class TestStaleness:
    @pytest.mark.asyncio
    async def test_a_stale_catalogue_still_answers_immediately(self, tmp_path):
        catalogue = _catalogue(tmp_path, fallback={"vendor/a": {}}, ttl_seconds=0)

        model = await catalogue.find("vendor/a")

        assert model is not None

    @pytest.mark.asyncio
    async def test_a_fresh_catalogue_is_not_stale(self, tmp_path, monkeypatch):
        catalogue = _catalogue(tmp_path, ttl_seconds=3600)
        _patch_fetch(monkeypatch, _api_payload({"vendor/a": True}))

        await catalogue.refresh()

        assert catalogue.is_stale() is False


class TestFailureCooldown:
    @pytest.mark.asyncio
    async def test_repeated_failed_refreshes_make_one_request(self, tmp_path, monkeypatch):
        catalogue = _catalogue(tmp_path, fallback={"vendor/a": {}})
        calls = _patch_failing_fetch(monkeypatch)

        for _ in range(5):
            assert await catalogue.refresh() is False

        assert calls["count"] == 1

    @pytest.mark.asyncio
    async def test_concurrent_failed_refreshes_make_one_request(self, tmp_path, monkeypatch):
        catalogue = _catalogue(tmp_path, fallback={"vendor/a": {}})
        calls = _patch_failing_fetch(monkeypatch)

        await asyncio.gather(*(catalogue.refresh() for _ in range(5)))

        assert calls["count"] == 1

    @pytest.mark.asyncio
    async def test_a_stale_catalogue_does_not_refetch_during_the_cooldown(
        self, tmp_path, monkeypatch
    ):
        catalogue = _catalogue(tmp_path, fallback={"vendor/a": {}}, ttl_seconds=0)
        calls = _patch_failing_fetch(monkeypatch)

        for _ in range(5):
            await catalogue.find("vendor/a")
            await _drain_background(catalogue)

        assert calls["count"] == 1

    @pytest.mark.asyncio
    async def test_an_unknown_model_lookup_makes_one_request_not_two(
        self, tmp_path, monkeypatch
    ):
        catalogue = _catalogue(tmp_path, fallback={"vendor/a": {}}, ttl_seconds=0)
        calls = _patch_failing_fetch(monkeypatch)

        assert await catalogue.find("vendor/unknown") is None
        await _drain_background(catalogue)

        assert calls["count"] == 1

    @pytest.mark.asyncio
    async def test_the_cooldown_expires_and_one_more_attempt_is_made(
        self, tmp_path, monkeypatch
    ):
        catalogue = _catalogue(tmp_path, fallback={"vendor/a": {}})
        calls = _patch_failing_fetch(monkeypatch)

        assert await catalogue.refresh() is False
        assert await catalogue.refresh() is False
        assert calls["count"] == 1

        catalogue._last_failed_fetch_at -= model_capabilities.FAILED_FETCH_RETRY_SECONDS + 1
        assert await catalogue.refresh() is False

        assert calls["count"] == 2

    @pytest.mark.asyncio
    async def test_a_success_clears_the_cooldown(self, tmp_path, monkeypatch):
        catalogue = _catalogue(tmp_path, fallback={"vendor/a": {}}, ttl_seconds=0)
        _patch_failing_fetch(monkeypatch)
        await catalogue.refresh()
        assert catalogue.is_in_failure_cooldown() is True

        catalogue._last_failed_fetch_at = None
        _patch_fetch(monkeypatch, _api_payload({"vendor/a": True}))
        assert await catalogue.refresh() is True

        assert catalogue.is_in_failure_cooldown() is False


async def _drain_background(catalogue):
    task = catalogue._background_refresh
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


def _patch_failing_fetch(monkeypatch):
    calls = {"count": 0}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            calls["count"] += 1
            await asyncio.sleep(0.01)
            raise ConnectionError("openrouter unreachable")

    monkeypatch.setattr(
        "src.autobots.conn.openrouter.model_capabilities.httpx.AsyncClient", Client
    )
    return calls


def _patch_fetch(monkeypatch, payload):
    calls = {"count": 0}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            calls["count"] += 1
            await asyncio.sleep(0.01)
            return Response()

    monkeypatch.setattr(
        "src.autobots.conn.openrouter.model_capabilities.httpx.AsyncClient", Client
    )
    return calls
