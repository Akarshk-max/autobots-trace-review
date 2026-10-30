from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.autobots.conn.azure.azure_blob_storage import AzureBlobStorage
from src.autobots.data_model.context import Context

pytestmark = pytest.mark.asyncio


def make_storage() -> tuple[AzureBlobStorage, MagicMock]:
    storage = AzureBlobStorage.__new__(AzureBlobStorage)
    storage.account_url = "https://account.blob.core.windows.net"
    storage._account_key = "test-key"
    storage._account_name = "account"
    storage.container_name = "container"
    storage.is_system_storage = True
    storage.object_prefix = ""

    container = MagicMock()
    storage._container_client = container
    storage._service_client = MagicMock()
    return storage, container


async def test_sync_adapter_upload_runs_blocking_sdk_call():
    storage, container = make_storage()

    blob = MagicMock()
    blob.url = "https://account.blob.core.windows.net/container/path/file.txt"
    container.get_blob_client.return_value = blob

    file_obj = BytesIO(b"payload")
    url = await storage.upload_fileobj_retry(
        Context(),
        file_obj,
        "path/file.txt",
    )

    container.get_blob_client.assert_called_once_with("path/file.txt")
    blob.upload_blob.assert_called_once()
    kwargs = blob.upload_blob.call_args.kwargs
    assert kwargs["data"] is file_obj
    assert kwargs["overwrite"] is True
    assert url == blob.url


async def test_sync_adapter_download_streams_chunks():
    storage, container = make_storage()

    downloader = MagicMock()
    downloader.chunks.return_value = iter([b"abc", b"def"])
    container.download_blob.return_value = downloader

    output = BytesIO()
    await storage.download_key_to_fileobj(
        Context(),
        "/path/file.bin",
        output,
    )

    container.download_blob.assert_called_once_with("path/file.bin")
    assert output.getvalue() == b"abcdef"


async def test_sync_adapter_get_bytes_reads_sync_downloader():
    storage, container = make_storage()

    downloader = MagicMock()
    downloader.readall.return_value = b"contents"
    container.download_blob.return_value = downloader

    result = await storage.get_bytes(Context(), "/file.bin")

    container.download_blob.assert_called_once_with("file.bin")
    downloader.readall.assert_called_once_with()
    assert result == b"contents"


async def test_sync_adapter_lists_objects_with_limit():
    storage, container = make_storage()
    container.list_blobs.return_value = iter(
        [
            SimpleNamespace(name="prefix/", size=0, etag="folder"),
            SimpleNamespace(name="prefix/a.txt", size=1, etag="a"),
            SimpleNamespace(name="prefix/b.txt", size=2, etag="b"),
            SimpleNamespace(name="prefix/c.txt", size=3, etag="c"),
        ]
    )

    objects = await storage.list(prefix="prefix/", limit=2)

    container.list_blobs.assert_called_once_with(name_starts_with="prefix/")
    assert [obj.key for obj in objects] == [
        "prefix/a.txt",
        "prefix/b.txt",
    ]
    assert [obj.size for obj in objects] == [1, 2]


async def test_sync_adapter_exists_uses_sync_blob_client():
    storage, container = make_storage()

    blob = MagicMock()
    blob.exists.return_value = True
    container.get_blob_client.return_value = blob

    assert await storage.exists("/file.txt") is True

    container.get_blob_client.assert_called_once_with("file.txt")
    blob.exists.assert_called_once_with()


async def test_sync_adapter_delete_uses_sync_container_client():
    storage, container = make_storage()

    deleted = await storage.delete_key("/file.txt")

    container.delete_blob.assert_called_once_with("file.txt")
    assert deleted == [{"Key": "file.txt"}]


async def test_sync_adapter_copy_polls_until_success(monkeypatch):
    storage, container = make_storage()

    source = MagicMock()
    source.url = "https://account.blob.core.windows.net/container/source.txt"

    destination = MagicMock()
    destination.get_blob_properties.side_effect = [
        SimpleNamespace(
            copy=SimpleNamespace(
                status="pending",
                status_description="",
            )
        ),
        SimpleNamespace(
            copy=SimpleNamespace(
                status="success",
                status_description="",
            )
        ),
    ]

    container.get_blob_client.side_effect = [source, destination]

    sleep = MagicMock()

    async def fake_sleep(seconds):
        sleep(seconds)

    monkeypatch.setattr(
        "src.autobots.conn.azure.azure_blob_storage.asyncio.sleep",
        fake_sleep,
    )
    monkeypatch.setattr(
        "src.autobots.conn.azure.azure_blob_storage.generate_blob_sas",
        lambda **kwargs: "read-sas",
    )

    await storage.copy("source.txt", "destination.txt")

    assert container.get_blob_client.call_args_list == [
        (("source.txt",), {}),
        (("destination.txt",), {}),
    ]
    destination.start_copy_from_url.assert_called_once_with(f"{source.url}?read-sas")
    assert destination.get_blob_properties.call_count == 2
    sleep.assert_called_once_with(0.25)


async def test_sync_adapter_close_uses_sync_service_client():
    storage, _ = make_storage()

    await storage.close()

    storage._service_client.close.assert_called_once_with()
