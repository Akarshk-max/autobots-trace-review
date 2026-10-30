import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.autobots.conn.object_storage import storage_file_resolver as resolver
from src.autobots.data_model.context import Context

pytestmark = pytest.mark.asyncio


def make_storage(
    *,
    provider: str,
    container: str,
    owned_key: str | None,
    payload: bytes = b"secret",
):
    storage = MagicMock()
    storage.provider = provider
    storage.container_name = container
    storage.object_prefix = ""
    storage.is_system_storage = True
    storage.get_key_if_owned_url = AsyncMock(return_value=owned_key)
    storage.get_bytes = AsyncMock(return_value=payload)

    async def download(*, ctx, key, fileobj):
        del ctx, key
        fileobj.write(payload)

    storage.download_key_to_fileobj = AsyncMock(side_effect=download)
    return storage


async def test_local_path_is_unchanged_and_not_deleted(tmp_path):
    path = tmp_path / "key.pem"
    path.write_bytes(b"local-secret")

    async with resolver.resolve_local_or_storage_file(
        ctx=Context(),
        path_or_url=str(path),
        suffix=".pem",
    ) as resolved:
        assert resolved == str(path)
        assert path.exists()

    assert path.exists()
    assert path.read_bytes() == b"local-secret"


async def test_private_azure_url_uses_private_storage_and_cleans_temp(
    monkeypatch,
):
    url = "https://account.blob.core.windows.net/private-container/workday/key.pem"
    private = make_storage(
        provider="azure",
        container="private-container",
        owned_key="workday/key.pem",
        payload=b"azure-private",
    )
    public = make_storage(
        provider="azure",
        container="public-container",
        owned_key=None,
    )

    monkeypatch.setattr(
        resolver,
        "get_storage",
        lambda object_prefix="": private,
    )
    monkeypatch.setattr(
        resolver,
        "get_public_storage",
        lambda object_prefix="": public,
    )

    temp_path = None
    async with resolver.resolve_local_or_storage_file(
        ctx=Context(),
        path_or_url=url,
        suffix=".pem",
    ) as resolved:
        temp_path = resolved
        assert os.path.exists(resolved)
        assert open(resolved, "rb").read() == b"azure-private"
        assert stat.S_IMODE(os.stat(resolved).st_mode) == 0o600

    assert temp_path is not None
    assert not os.path.exists(temp_path)
    private.get_key_if_owned_url.assert_awaited_once_with(url)
    public.get_key_if_owned_url.assert_not_awaited()
    private.download_key_to_fileobj.assert_awaited_once()
    assert private.download_key_to_fileobj.await_args.kwargs["key"] == "workday/key.pem"


async def test_public_azure_url_falls_back_to_public_storage(
    monkeypatch,
):
    url = "https://account.blob.core.windows.net/public-container/adp/cert.pem?sig=not-logged"
    private = make_storage(
        provider="azure",
        container="private-container",
        owned_key=None,
    )
    public = make_storage(
        provider="azure",
        container="public-container",
        owned_key="adp/cert.pem",
        payload=b"azure-public",
    )

    monkeypatch.setattr(
        resolver,
        "get_storage",
        lambda object_prefix="": private,
    )
    monkeypatch.setattr(
        resolver,
        "get_public_storage",
        lambda object_prefix="": public,
    )

    async with resolver.resolve_local_or_storage_file(
        ctx=Context(),
        path_or_url=url,
        suffix=".pem",
    ) as resolved:
        assert open(resolved, "rb").read() == b"azure-public"

    private.get_key_if_owned_url.assert_awaited_once_with(url)
    public.get_key_if_owned_url.assert_awaited_once_with(url)
    public.download_key_to_fileobj.assert_awaited_once()


async def test_native_s3_private_bucket_resolves_on_s3_deployment(
    monkeypatch,
):
    private = make_storage(
        provider="s3",
        container="private-bucket",
        owned_key=None,
        payload=b"s3-private",
    )
    public = make_storage(
        provider="s3",
        container="public-bucket",
        owned_key=None,
    )

    monkeypatch.setattr(
        resolver,
        "get_storage",
        lambda object_prefix="": private,
    )
    monkeypatch.setattr(
        resolver,
        "get_public_storage",
        lambda object_prefix="": public,
    )

    async with resolver.resolve_local_or_storage_file(
        ctx=Context(),
        path_or_url="s3://private-bucket/workday/key.pem",
        suffix=".pem",
    ) as resolved:
        assert open(resolved, "rb").read() == b"s3-private"

    private.download_key_to_fileobj.assert_awaited_once()
    assert private.download_key_to_fileobj.await_args.kwargs["key"] == "workday/key.pem"


async def test_native_s3_public_bucket_resolves_on_s3_deployment(
    monkeypatch,
):
    private = make_storage(
        provider="s3",
        container="private-bucket",
        owned_key=None,
    )
    public = make_storage(
        provider="s3",
        container="public-bucket",
        owned_key=None,
        payload=b"s3-public",
    )

    monkeypatch.setattr(
        resolver,
        "get_storage",
        lambda object_prefix="": private,
    )
    monkeypatch.setattr(
        resolver,
        "get_public_storage",
        lambda object_prefix="": public,
    )

    async with resolver.resolve_local_or_storage_file(
        ctx=Context(),
        path_or_url="s3://public-bucket/adp/cert.pem",
        suffix=".pem",
    ) as resolved:
        assert open(resolved, "rb").read() == b"s3-public"

    private.download_key_to_fileobj.assert_not_awaited()
    public.download_key_to_fileobj.assert_awaited_once()


async def test_unknown_s3_bucket_preserves_legacy_private_fallback(
    monkeypatch,
):
    private = make_storage(
        provider="s3",
        container="private-bucket",
        owned_key=None,
        payload=b"legacy-private",
    )
    public = make_storage(
        provider="s3",
        container="public-bucket",
        owned_key=None,
    )

    monkeypatch.setattr(
        resolver,
        "get_storage",
        lambda object_prefix="": private,
    )
    monkeypatch.setattr(
        resolver,
        "get_public_storage",
        lambda object_prefix="": public,
    )

    async with resolver.resolve_local_or_storage_file(
        ctx=Context(),
        path_or_url="s3://historical-name/adp/key.pem",
        suffix=".pem",
    ) as resolved:
        assert open(resolved, "rb").read() == b"legacy-private"

    private.download_key_to_fileobj.assert_awaited_once()
    assert private.download_key_to_fileobj.await_args.kwargs["key"] == "adp/key.pem"


async def test_s3_uri_is_rejected_on_azure_deployment(monkeypatch):
    private = make_storage(
        provider="azure",
        container="private-container",
        owned_key=None,
    )
    public = make_storage(
        provider="azure",
        container="public-container",
        owned_key=None,
    )

    monkeypatch.setattr(
        resolver,
        "get_storage",
        lambda object_prefix="": private,
    )
    monkeypatch.setattr(
        resolver,
        "get_public_storage",
        lambda object_prefix="": public,
    )

    with pytest.raises(
        ValueError,
        match="unavailable for the configured object-storage provider",
    ):
        async with resolver.resolve_local_or_storage_file(
            ctx=Context(),
            path_or_url="s3://bucket/key.pem",
        ):
            pass


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/key.pem",
        "ftp://example.com/key.pem",
        "file:///etc/passwd",
    ],
)
async def test_unsafe_or_unsupported_schemes_are_rejected(
    monkeypatch,
    url,
):
    private = make_storage(
        provider="azure",
        container="private-container",
        owned_key=None,
    )

    monkeypatch.setattr(
        resolver,
        "get_storage",
        lambda object_prefix="": private,
    )

    with pytest.raises(ValueError):
        async with resolver.resolve_local_or_storage_file(
            ctx=Context(),
            path_or_url=url,
        ):
            pass


async def test_foreign_https_url_is_rejected(monkeypatch):
    private = make_storage(
        provider="azure",
        container="private-container",
        owned_key=None,
    )
    public = make_storage(
        provider="azure",
        container="public-container",
        owned_key=None,
    )

    monkeypatch.setattr(
        resolver,
        "get_storage",
        lambda object_prefix="": private,
    )
    monkeypatch.setattr(
        resolver,
        "get_public_storage",
        lambda object_prefix="": public,
    )

    with pytest.raises(ValueError, match="does not belong"):
        async with resolver.resolve_local_or_storage_file(
            ctx=Context(),
            path_or_url="https://example.com/private-key.pem",
        ):
            pass


async def test_empty_and_oversized_objects_are_rejected(monkeypatch):
    private = make_storage(
        provider="azure",
        container="private-container",
        owned_key="key.pem",
        payload=b"",
    )

    monkeypatch.setattr(
        resolver,
        "get_storage",
        lambda object_prefix="": private,
    )

    with pytest.raises(ValueError, match="empty"):
        async with resolver.resolve_local_or_storage_file(
            ctx=Context(),
            path_or_url=("https://account.blob.core.windows.net/private-container/key.pem"),
        ):
            pass

    async def oversized_download(*, ctx, key, fileobj):
        del ctx, key
        fileobj.write(b"12345")

    private.download_key_to_fileobj = AsyncMock(side_effect=oversized_download)

    with pytest.raises(ValueError, match="exceeds"):
        async with resolver.resolve_local_or_storage_file(
            ctx=Context(),
            path_or_url=("https://account.blob.core.windows.net/private-container/key.pem"),
            max_bytes=4,
        ):
            pass


async def test_cleanup_occurs_when_context_body_raises(monkeypatch):
    private = make_storage(
        provider="azure",
        container="private-container",
        owned_key="workday/key.pem",
        payload=b"private-key",
    )

    monkeypatch.setattr(
        resolver,
        "get_storage",
        lambda object_prefix="": private,
    )

    temp_path = None
    with pytest.raises(RuntimeError, match="caller failed"):
        async with resolver.resolve_local_or_storage_file(
            ctx=Context(),
            path_or_url=("https://account.blob.core.windows.net/private-container/workday/key.pem"),
        ) as resolved:
            temp_path = resolved
            assert os.path.exists(resolved)
            raise RuntimeError("caller failed")

    assert temp_path is not None
    assert not os.path.exists(temp_path)


async def test_partial_file_is_removed_when_download_fails(
    monkeypatch,
    tmp_path,
):
    private = make_storage(
        provider="azure",
        container="private-container",
        owned_key="workday/key.pem",
    )

    async def failing_download(*, ctx, key, fileobj):
        del ctx, key
        fileobj.write(b"partial")
        raise RuntimeError("storage failed")

    private.download_key_to_fileobj = AsyncMock(side_effect=failing_download)

    monkeypatch.setattr(
        resolver,
        "get_storage",
        lambda object_prefix="": private,
    )

    before = set(Path(tempfile.gettempdir()).glob("*.pem"))

    with pytest.raises(ValueError, match="Unable to download"):
        async with resolver.resolve_local_or_storage_file(
            ctx=Context(),
            path_or_url=("https://account.blob.core.windows.net/private-container/workday/key.pem"),
            suffix=".pem",
        ):
            pass

    after = set(Path(tempfile.gettempdir()).glob("*.pem"))
    assert after == before


async def test_private_s3_https_url_uses_private_storage(
    monkeypatch,
):
    url = "https://private-bucket.s3.us-east-1.amazonaws.com/workday/key.pem"
    private = make_storage(
        provider="s3",
        container="private-bucket",
        owned_key="workday/key.pem",
        payload=b"s3-private-https",
    )
    public = make_storage(
        provider="s3",
        container="public-bucket",
        owned_key=None,
    )

    monkeypatch.setattr(
        resolver,
        "get_storage",
        lambda object_prefix="": private,
    )
    monkeypatch.setattr(
        resolver,
        "get_public_storage",
        lambda object_prefix="": public,
    )

    async with resolver.resolve_local_or_storage_file(
        ctx=Context(),
        path_or_url=url,
        suffix=".pem",
    ) as resolved:
        assert open(resolved, "rb").read() == b"s3-private-https"

    private.get_key_if_owned_url.assert_awaited_once_with(url)
    private.download_key_to_fileobj.assert_awaited_once()


async def test_public_s3_https_url_uses_public_storage(
    monkeypatch,
):
    url = "https://public-bucket.s3.us-east-1.amazonaws.com/adp/cert.pem"
    private = make_storage(
        provider="s3",
        container="private-bucket",
        owned_key=None,
    )
    public = make_storage(
        provider="s3",
        container="public-bucket",
        owned_key="adp/cert.pem",
        payload=b"s3-public-https",
    )

    monkeypatch.setattr(
        resolver,
        "get_storage",
        lambda object_prefix="": private,
    )
    monkeypatch.setattr(
        resolver,
        "get_public_storage",
        lambda object_prefix="": public,
    )

    async with resolver.resolve_local_or_storage_file(
        ctx=Context(),
        path_or_url=url,
        suffix=".pem",
    ) as resolved:
        assert open(resolved, "rb").read() == b"s3-public-https"

    private.get_key_if_owned_url.assert_awaited_once_with(url)
    public.get_key_if_owned_url.assert_awaited_once_with(url)
    public.download_key_to_fileobj.assert_awaited_once()
