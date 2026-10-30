import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.autobots.files import file_downloader as downloader

pytestmark = pytest.mark.asyncio


class FailingAsyncClient:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("HTTP access unavailable")


def make_storage(
    *,
    provider: str,
    container: str,
    owned_key: str | None,
    payload: bytes,
    is_system_storage: bool = True,
) -> MagicMock:
    storage = MagicMock()
    storage.provider = provider
    storage.container_name = container
    storage.is_system_storage = is_system_storage
    storage.get_key_if_owned_url = AsyncMock(return_value=owned_key)
    storage.get_key = AsyncMock(return_value=owned_key)

    async def download(*, ctx, key, fileobj):
        fileobj.write(payload)

    storage.download_key_to_fileobj = AsyncMock(side_effect=download)
    return storage


async def run_download(monkeypatch, url: str):
    monkeypatch.setattr(
        downloader.httpx,
        "AsyncClient",
        FailingAsyncClient,
    )

    result = await downloader.FileDownloader._download_urls_to_tmpfiles(
        ctx=MagicMock(),
        user=MagicMock(),
        db=MagicMock(),
        urls=[url],
    )
    return result


def read_and_remove(result: dict[str, str], url: str) -> bytes:
    path = result[url]
    try:
        with open(path, "rb") as file:
            return file.read()
    finally:
        if os.path.exists(path):
            os.remove(path)


async def test_azure_public_url_uses_public_storage_fallback(
    monkeypatch,
):
    url = "https://account.blob.core.windows.net/public-container/files/document.pdf"
    public = make_storage(
        provider="azure",
        container="public-container",
        owned_key="files/document.pdf",
        payload=b"azure-public",
    )
    private = make_storage(
        provider="azure",
        container="private-container",
        owned_key=None,
        payload=b"",
    )

    monkeypatch.setattr(
        downloader,
        "get_public_storage",
        lambda object_prefix="": public,
    )
    monkeypatch.setattr(
        downloader,
        "get_storage",
        lambda object_prefix="": private,
    )

    result = await run_download(monkeypatch, url)

    assert read_and_remove(result, url) == b"azure-public"
    public.download_key_to_fileobj.assert_awaited_once()
    private.get_key_if_owned_url.assert_not_awaited()


async def test_azure_private_url_uses_private_storage_fallback(
    monkeypatch,
):
    url = "https://account.blob.core.windows.net/private-container/files/document.pdf"
    public = make_storage(
        provider="azure",
        container="public-container",
        owned_key=None,
        payload=b"",
    )
    private = make_storage(
        provider="azure",
        container="private-container",
        owned_key="files/document.pdf",
        payload=b"azure-private",
    )

    monkeypatch.setattr(
        downloader,
        "get_public_storage",
        lambda object_prefix="": public,
    )
    monkeypatch.setattr(
        downloader,
        "get_storage",
        lambda object_prefix="": private,
    )

    result = await run_download(monkeypatch, url)

    assert read_and_remove(result, url) == b"azure-private"
    public.get_key_if_owned_url.assert_awaited_once_with(url)
    private.get_key_if_owned_url.assert_awaited_once_with(url)
    private.download_key_to_fileobj.assert_awaited_once()


async def test_native_s3_uri_uses_configured_deployment_storage(
    monkeypatch,
):
    url = "s3://public-bucket/files/document.pdf"
    public = make_storage(
        provider="s3",
        container="public-bucket",
        owned_key="files/document.pdf",
        payload=b"system-s3",
    )
    private = make_storage(
        provider="s3",
        container="private-bucket",
        owned_key=None,
        payload=b"",
    )

    monkeypatch.setattr(
        downloader,
        "get_public_storage",
        lambda object_prefix="": public,
    )
    monkeypatch.setattr(
        downloader,
        "get_storage",
        lambda object_prefix="": private,
    )
    customer_creds = AsyncMock()
    monkeypatch.setattr(
        downloader.UserAppCreds,
        "get_app_creds_by_app_name",
        customer_creds,
    )

    result = await downloader.FileDownloader._download_urls_to_tmpfiles(
        ctx=MagicMock(),
        user=MagicMock(),
        db=MagicMock(),
        urls=[url],
    )

    assert read_and_remove(result, url) == b"system-s3"
    public.get_key.assert_awaited_once_with(url)
    public.download_key_to_fileobj.assert_awaited_once()
    customer_creds.assert_not_awaited()


async def test_customer_s3_uri_uses_customer_credentials(
    monkeypatch,
):
    url = "s3://customer-bucket/files/document.pdf"
    public = make_storage(
        provider="s3",
        container="public-bucket",
        owned_key=None,
        payload=b"",
    )
    private = make_storage(
        provider="s3",
        container="private-bucket",
        owned_key=None,
        payload=b"",
    )
    customer = make_storage(
        provider="s3",
        container="customer-bucket",
        owned_key="files/document.pdf",
        payload=b"customer-s3",
        is_system_storage=False,
    )

    monkeypatch.setattr(
        downloader,
        "get_public_storage",
        lambda object_prefix="": public,
    )
    monkeypatch.setattr(
        downloader,
        "get_storage",
        lambda object_prefix="": private,
    )
    monkeypatch.setattr(
        downloader.UserAppCreds,
        "get_app_creds_by_app_name",
        AsyncMock(
            return_value=SimpleNamespace(
                creds={
                    "region_name": "us-east-1",
                    "aws_access_key_id": "access",
                    "aws_secret_access_key": "secret",
                    "bucket_name": "customer-bucket",
                    "object_prefix": "",
                }
            )
        ),
    )
    customer_factory = MagicMock(return_value=customer)
    monkeypatch.setattr(
        downloader,
        "get_aws_s3",
        customer_factory,
    )

    result = await downloader.FileDownloader._download_urls_to_tmpfiles(
        ctx=MagicMock(),
        user=MagicMock(),
        db=MagicMock(),
        urls=[url],
    )

    assert read_and_remove(result, url) == b"customer-s3"
    customer.get_key.assert_awaited_once_with(url)
    customer.download_key_to_fileobj.assert_awaited_once()
    customer_factory.assert_called_once()


async def test_foreign_https_url_is_not_sent_to_customer_aws(
    monkeypatch,
):
    url = "https://foreign.example/document.pdf"
    public = make_storage(
        provider="azure",
        container="public-container",
        owned_key=None,
        payload=b"",
    )
    private = make_storage(
        provider="azure",
        container="private-container",
        owned_key=None,
        payload=b"",
    )

    monkeypatch.setattr(
        downloader,
        "get_public_storage",
        lambda object_prefix="": public,
    )
    monkeypatch.setattr(
        downloader,
        "get_storage",
        lambda object_prefix="": private,
    )
    customer_creds = AsyncMock()
    customer_factory = MagicMock()
    monkeypatch.setattr(
        downloader.UserAppCreds,
        "get_app_creds_by_app_name",
        customer_creds,
    )
    monkeypatch.setattr(
        downloader,
        "get_aws_s3",
        customer_factory,
    )

    result = await run_download(monkeypatch, url)

    assert result == {}
    customer_creds.assert_not_awaited()
    customer_factory.assert_not_called()
