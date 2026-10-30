from __future__ import annotations

import asyncio
import mimetypes
import os
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import IO, Any
from urllib.parse import unquote, urlparse

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
)
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from src.autobots.conn.object_storage.models import SignedUpload, StorageObject
from src.autobots.data_model.context import Context


class AzureBlobStorage:
    """Azure Blob Storage implementation of the system object-storage contract."""

    provider = "azure"

    def __init__(
        self,
        *,
        account_url: str,
        account_key: str,
        container_name: str,
        object_prefix: str = "default",
        is_system_storage: bool = True,
    ) -> None:
        if not account_url:
            raise ValueError("Azure Storage account URL is required")
        if not account_key:
            raise ValueError("Azure Storage account key is required")
        if not container_name:
            raise ValueError("Azure Storage container name is required")

        self.account_url = account_url.rstrip("/")
        self._account_key = account_key
        self.container_name = container_name
        self.is_system_storage = is_system_storage
        self.set_object_prefix(object_prefix)

        self._service_client = BlobServiceClient(
            account_url=self.account_url,
            credential=self._account_key,
        )
        self._account_name = self._service_client.account_name
        if not self._account_name:
            raise ValueError("Could not determine Azure Storage account name")

        self._container_client = self._service_client.get_container_client(self.container_name)

    def set_object_prefix(self, object_prefix: str) -> None:
        normalized = (object_prefix or "").strip("/")
        self.object_prefix = f"{normalized}/" if normalized else ""

    def _resolve_filename(self, filename: str) -> str:
        filename = (filename or "").lstrip("/")
        if self.object_prefix and not filename.startswith(self.object_prefix):
            return f"{self.object_prefix}{filename}"
        return filename

    @staticmethod
    def _normalize_key(key: str) -> str:
        return (key or "").lstrip("/")

    @staticmethod
    def _content_settings(filename: str) -> ContentSettings:
        extension = Path(filename).suffix.lower()
        base_name = os.path.basename(filename)
        guessed_type, _ = mimetypes.guess_type(filename)
        content_type = guessed_type or "application/octet-stream"

        if extension in {".html", ".htm"}:
            return ContentSettings(content_type="text/html; charset=utf-8")

        if extension in {
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
            ".bmp",
            ".tiff",
            ".tif",
            ".svg",
            ".pdf",
        }:
            return ContentSettings(
                content_type=content_type,
                content_disposition=f'inline; filename="{base_name}"',
            )

        return ContentSettings(
            content_type=content_type,
            content_disposition=f'attachment; filename="{base_name}"',
        )

    async def put(self, ctx: Context, data: str, filename: str) -> int:
        encoded = data.encode("utf-8")
        try:
            uploaded_url = await self.upload_fileobj(ctx, BytesIO(encoded), filename)
            return len(data) if uploaded_url else -1
        except Exception:
            logger.bind(
                ctx=ctx,
                provider=self.provider,
                container=self.container_name,
                key=filename,
            ).exception("Object-storage text upload failed")
            return -1

    async def put_file_obj(self, file_obj: IO[bytes], filename: str) -> str:
        return await self.upload_fileobj(Context(), file_obj, filename)

    async def upload_fileobj(
        self,
        ctx: Context,
        file_obj: IO[bytes] | Any,
        filename: str,
    ) -> str:
        try:
            return await self.upload_fileobj_retry(ctx, file_obj, filename)
        except Exception:
            logger.bind(
                ctx=ctx,
                provider=self.provider,
                container=self.container_name,
                key=filename,
            ).exception("Object-storage upload failed")
            return ""

    @retry(
        wait=wait_exponential_jitter(initial=0.5, max=10),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def upload_fileobj_retry(
        self,
        ctx: Context,
        file_obj: IO[bytes] | Any,
        filename: str,
    ) -> str:
        key = self._resolve_filename(filename)
        blob_client = self._container_client.get_blob_client(key)

        await asyncio.to_thread(
            blob_client.upload_blob,
            data=file_obj,
            overwrite=True,
            content_settings=self._content_settings(filename),
        )
        return blob_client.url

    async def download_fileobj(
        self,
        ctx: Context,
        filename: str,
        fileobj: IO[bytes],
    ) -> str:
        key = self._resolve_filename(filename)
        await self.download_key_to_fileobj(ctx, key, fileobj)
        return await self.get_url(key)

    async def download_key_to_fileobj(
        self,
        ctx: Context,
        key: str,
        fileobj: IO[bytes],
    ) -> None:
        normalized_key = self._normalize_key(key)
        try:

            def _download() -> None:
                downloader = self._container_client.download_blob(normalized_key)
                for chunk in downloader.chunks():
                    fileobj.write(chunk)

            await asyncio.to_thread(_download)
        except Exception:
            logger.bind(
                ctx=ctx,
                provider=self.provider,
                container=self.container_name,
                key=normalized_key,
            ).exception("Object-storage download failed")
            raise

    async def get(self, filename: str) -> str | None:
        key = self._resolve_filename(filename)
        try:

            def _read_text() -> str:
                downloader = self._container_client.download_blob(key)
                return downloader.readall().decode("utf-8")

            return await asyncio.to_thread(_read_text)
        except Exception:
            logger.bind(
                provider=self.provider,
                container=self.container_name,
                key=key,
            ).exception("Object-storage text download failed")
            return None

    async def get_bytes(self, ctx: Context, key: str) -> bytes | None:
        normalized_key = self._normalize_key(key)
        try:

            def _read_bytes() -> bytes:
                downloader = self._container_client.download_blob(normalized_key)
                return downloader.readall()

            return await asyncio.to_thread(_read_bytes)
        except ResourceNotFoundError:
            return None
        except Exception:
            logger.bind(
                ctx=ctx,
                provider=self.provider,
                container=self.container_name,
                key=normalized_key,
            ).exception("Object-storage byte download failed")
            return None

    async def list(
        self,
        prefix: str | None = None,
        limit: int = 300,
    ) -> list[StorageObject]:
        if not prefix:
            full_prefix = self.object_prefix
        else:
            normalized_prefix = prefix.lstrip("/")
            full_prefix = (
                normalized_prefix
                if normalized_prefix.startswith(self.object_prefix)
                else f"{self.object_prefix}{normalized_prefix}"
            )

        def _list_objects() -> list[StorageObject]:
            objects: list[StorageObject] = []
            for blob in self._container_client.list_blobs(name_starts_with=full_prefix):
                if blob.name.endswith("/"):
                    continue
                objects.append(
                    StorageObject(
                        key=blob.name,
                        size=getattr(blob, "size", None),
                        etag=getattr(blob, "etag", None),
                    )
                )
                if len(objects) >= limit:
                    break
            return objects

        return await asyncio.to_thread(_list_objects)

    async def delete(self, filename: str) -> list[dict[str, Any]]:
        return await self.delete_key(self._resolve_filename(filename))

    async def delete_key(self, key: str) -> list[dict[str, Any]]:
        key = self._normalize_key(key)
        try:
            await asyncio.to_thread(self._container_client.delete_blob, key)
            return [{"Key": key}]
        except ResourceNotFoundError:
            return []
        except Exception:
            logger.bind(
                provider=self.provider,
                container=self.container_name,
                key=key,
            ).exception("Object-storage delete failed")
            return []

    async def delete_prefix(self, prefix: str) -> list[dict[str, Any]]:
        normalized_prefix = self._normalize_key(prefix)
        objects = await self.list(prefix=normalized_prefix, limit=100_000)
        deleted: list[dict[str, Any]] = []
        for storage_object in objects:
            deleted.extend(await self.delete_key(storage_object.key))
        return deleted

    async def exists(self, key: str) -> bool:
        blob_client = self._container_client.get_blob_client(self._normalize_key(key))
        return await asyncio.to_thread(blob_client.exists)

    async def copy(self, source_key: str, destination_key: str) -> None:
        source_key = self._normalize_key(source_key)
        destination_key = self._normalize_key(destination_key)

        source_blob = self._container_client.get_blob_client(source_key)
        destination_blob = self._container_client.get_blob_client(destination_key)

        now = datetime.now(timezone.utc)
        source_sas = generate_blob_sas(
            account_name=self._account_name,
            container_name=self.container_name,
            blob_name=source_key,
            account_key=self._account_key,
            permission=BlobSasPermissions(read=True),
            start=now - timedelta(minutes=5),
            expiry=now + timedelta(minutes=30),
            protocol="https",
        )

        await asyncio.to_thread(
            destination_blob.start_copy_from_url,
            f"{source_blob.url}?{source_sas}",
        )

        for _ in range(120):
            properties = await asyncio.to_thread(destination_blob.get_blob_properties)
            copy_status = getattr(getattr(properties, "copy", None), "status", None)
            if copy_status == "success":
                return
            if copy_status in {"failed", "aborted"}:
                description = getattr(
                    getattr(properties, "copy", None),
                    "status_description",
                    "",
                )
                raise RuntimeError(f"Azure blob copy failed: {copy_status}: {description}")
            await asyncio.sleep(0.25)

        raise TimeoutError(f"Timed out copying object '{source_key}' to '{destination_key}'")

    async def get_url(self, key: str) -> str:
        return self._container_client.get_blob_client(self._normalize_key(key)).url

    async def get_https_url(self, key: str) -> str:
        return await self.get_url(key)

    async def get_key_from_https_url(self, url: str) -> str:
        key = await self.get_key_if_owned_url(str(url))
        if key is None:
            raise ValueError("URL does not belong to configured Azure object storage")
        return key

    async def get_key_if_owned_url(self, url: str) -> str | None:
        parsed = urlparse(str(url))
        expected = urlparse(self.account_url)

        if parsed.scheme.lower() != "https":
            return None
        if parsed.netloc.lower() != expected.netloc.lower():
            return None

        path = unquote(parsed.path.lstrip("/"))
        expected_base_path = unquote(expected.path.strip("/"))

        # Standard Azure endpoints have no base path. Azurite-style endpoints
        # may include the account name in the endpoint path.
        if expected_base_path:
            if path == expected_base_path:
                return None
            prefix = f"{expected_base_path}/"
            if not path.startswith(prefix):
                return None
            path = path[len(prefix) :]

        container, separator, key = path.partition("/")
        if not separator or container != self.container_name:
            return None
        return key or None

    async def get_key(self, url: str) -> str:
        return await self.get_key_from_https_url(url)

    async def get_filename(self, key: str) -> str:
        return self._normalize_key(key).split("/")[-1]

    async def generate_signed_upload(
        self,
        key: str,
        expiration: int = 3600,
        *,
        upload_profile: str = "default",
    ) -> SignedUpload:
        del upload_profile

        key = self._normalize_key(key)
        now = datetime.now(timezone.utc)
        sas = generate_blob_sas(
            account_name=self._account_name,
            container_name=self.container_name,
            blob_name=key,
            account_key=self._account_key,
            permission=BlobSasPermissions(create=True, write=True),
            start=now - timedelta(minutes=5),
            expiry=now + timedelta(seconds=expiration),
            protocol="https",
        )

        public_url = await self.get_url(key)
        return SignedUpload(
            url=f"{public_url}?{sas}",
            public_url=public_url,
            key=key,
            headers={"x-ms-blob-type": "BlockBlob"},
        )

    async def generate_presigned_put_url_for_file(
        self,
        filename: str,
        expiration: int = 3600,
        **_: Any,
    ) -> tuple[str, str]:
        key = self._resolve_filename(filename)
        signed = await self.generate_signed_upload(key, expiration)
        return signed.url, signed.public_url

    async def close(self) -> None:
        await asyncio.to_thread(self._service_client.close)
