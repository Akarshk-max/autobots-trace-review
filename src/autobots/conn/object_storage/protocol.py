from __future__ import annotations

from typing import IO, Any, Literal, Protocol

from src.autobots.conn.object_storage.models import SignedUpload, StorageObject
from src.autobots.data_model.context import Context


class ObjectStorage(Protocol):
    """Application-facing contract for deployment-level object storage."""

    provider: Literal["s3", "azure"]
    container_name: str
    object_prefix: str
    is_system_storage: bool

    def set_object_prefix(self, object_prefix: str) -> None: ...

    async def put(self, ctx: Context, data: str, filename: str) -> int: ...

    async def put_file_obj(self, file_obj: IO[bytes], filename: str) -> str: ...

    async def upload_fileobj(
        self,
        ctx: Context,
        file_obj: IO[bytes] | Any,
        filename: str,
    ) -> str: ...

    async def download_fileobj(
        self,
        ctx: Context,
        filename: str,
        fileobj: IO[bytes],
    ) -> str: ...

    async def download_key_to_fileobj(
        self,
        ctx: Context,
        key: str,
        fileobj: IO[bytes],
    ) -> None: ...

    async def get(self, filename: str) -> str | None: ...

    async def get_bytes(self, ctx: Context, key: str) -> bytes | None: ...

    async def list(
        self,
        prefix: str | None = None,
        limit: int = 300,
    ) -> list[StorageObject] | list[Any]: ...

    async def delete(self, filename: str) -> list[dict[str, Any]]: ...

    async def delete_key(self, key: str) -> list[dict[str, Any]]: ...

    async def delete_prefix(self, prefix: str) -> list[dict[str, Any]]: ...

    async def copy(self, source_key: str, destination_key: str) -> None: ...

    async def exists(self, key: str) -> bool: ...

    async def get_url(self, key: str) -> str: ...

    async def get_https_url(self, key: str) -> str: ...

    async def get_key(self, url: str) -> str: ...

    async def get_key_from_https_url(self, url: str) -> str: ...

    async def get_key_if_owned_url(self, url: str) -> str | None: ...

    async def get_filename(self, key: str) -> str: ...

    async def generate_signed_upload(
        self,
        key: str,
        expiration: int = 3600,
        *,
        upload_profile: str = "default",
    ) -> SignedUpload: ...
