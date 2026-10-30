from __future__ import annotations

import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import unquote, urlparse

from loguru import logger

from src.autobots.conn.object_storage.factory import get_public_storage, get_storage
from src.autobots.conn.object_storage.protocol import ObjectStorage
from src.autobots.data_model.context import Context

DEFAULT_MAX_CREDENTIAL_FILE_BYTES = 10 * 1024 * 1024


def _is_s3_https_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        return False

    host = (parsed.hostname or "").lower()
    return (
        host == "s3.amazonaws.com"
        or (host.startswith("s3.") and host.endswith(".amazonaws.com"))
        or (".s3." in host and host.endswith(".amazonaws.com"))
        or host.endswith(".s3.amazonaws.com")
    )


def _safe_public_storage() -> ObjectStorage | None:
    """
    Return configured public storage when available.

    Some historical deployments may only have private storage configured.
    Private credential resolution must continue to work in that situation.
    """
    try:
        return get_public_storage(object_prefix="")
    except ValueError:
        return None


async def _resolve_storage_and_key(
    path_or_url: str,
) -> tuple[ObjectStorage, str]:
    parsed = urlparse(path_or_url)
    scheme = parsed.scheme.lower()

    # Reject unsafe or unsupported schemes before storage initialization so
    # the error does not depend on deployment configuration.
    if scheme == "http":
        raise ValueError("HTTP credential URLs are not supported")
    if scheme not in {"https", "s3"}:
        raise ValueError(f"Unsupported credential URL scheme: {scheme or 'unknown'}")

    private_storage = get_storage(object_prefix="")

    if scheme == "s3":
        bucket = (parsed.netloc or "").strip()
        key = unquote((parsed.path or "").lstrip("/"))

        if not bucket or not key:
            raise ValueError("Invalid S3 credential URI")

        if (
            private_storage.provider == "s3"
            and bucket.lower() == private_storage.container_name.lower()
        ):
            return private_storage, key

        public_storage = _safe_public_storage()
        if (
            public_storage is not None
            and public_storage.provider == "s3"
            and bucket.lower() == public_storage.container_name.lower()
        ):
            return public_storage, key

        if private_storage.provider != "s3":
            raise ValueError(
                "S3 credential URI is unavailable for the configured object-storage provider"
            )

        # Compatibility fallback:
        #
        # Historically ADP/Workday ignored an unmatched bucket in s3:// URLs
        # and attempted to read the key from the configured private deployment
        # bucket. Preserve that behavior for S3 deployments so existing saved
        # credentials do not unexpectedly stop working.
        logger.bind(provider="s3").warning(
            "Using legacy private-bucket fallback for an unmatched S3 credential URI"
        )
        return private_storage, key

    if scheme == "https":
        key = await private_storage.get_key_if_owned_url(path_or_url)
        if key is not None:
            return private_storage, key

        public_storage = _safe_public_storage()
        if public_storage is not None:
            key = await public_storage.get_key_if_owned_url(path_or_url)
            if key is not None:
                return public_storage, key

        # Preserve the historical S3 behavior for an unmatched S3 HTTPS URL:
        # get_key_from_https_url() used only the path and downloaded it from the
        # configured private deployment bucket.
        if private_storage.provider == "s3" and _is_s3_https_url(path_or_url):
            key = unquote((parsed.path or "").lstrip("/"))
            if not key:
                raise ValueError("Invalid S3 credential URL")

            logger.bind(provider="s3").warning(
                "Using legacy private-bucket fallback for an unmatched S3 credential URL"
            )
            return private_storage, key

        raise ValueError("Credential URL does not belong to configured deployment object storage")

    raise ValueError("Credential URL does not belong to configured deployment object storage")


class _BoundedCredentialWriter:
    """File-like writer that rejects credential objects exceeding a limit."""

    def __init__(self, fileobj, max_bytes: int):
        self._fileobj = fileobj
        self._max_bytes = max_bytes
        self.bytes_written = 0

    def write(self, data: bytes) -> int:
        next_size = self.bytes_written + len(data)
        if next_size > self._max_bytes:
            raise ValueError("Downloaded credential file exceeds the allowed size")

        written = self._fileobj.write(data)
        self.bytes_written += written
        return written

    def flush(self) -> None:
        self._fileobj.flush()

    def __getattr__(self, name):
        return getattr(self._fileobj, name)


async def download_storage_file_to_temp(
    *,
    ctx: Context,
    path_or_url: str,
    suffix: str = "",
    max_bytes: int = DEFAULT_MAX_CREDENTIAL_FILE_BYTES,
) -> str:
    """
    Download a deployment-owned object into a secure bounded temporary file.

    The caller owns the returned file and must delete it. Prefer
    resolve_local_or_storage_file() for normal use.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    storage, key = await _resolve_storage_and_key(path_or_url)

    fd = -1
    temp_path: str | None = None

    try:
        fd, temp_path = tempfile.mkstemp(suffix=suffix)
        os.fchmod(fd, 0o600)

        with os.fdopen(fd, "wb") as temp_file:
            fd = -1
            bounded_file = _BoundedCredentialWriter(
                temp_file,
                max_bytes=max_bytes,
            )

            try:
                await storage.download_key_to_fileobj(
                    ctx=ctx,
                    key=key,
                    fileobj=bounded_file,
                )
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError(
                    "Unable to download credential file from configured object storage"
                ) from exc

            bounded_file.flush()

            if bounded_file.bytes_written == 0:
                raise ValueError("Downloaded credential file is empty")

        return temp_path

    except Exception:
        if fd >= 0:
            os.close(fd)
        if temp_path:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                pass
        raise


@asynccontextmanager
async def resolve_local_or_storage_file(
    *,
    ctx: Context,
    path_or_url: str,
    suffix: str = "",
    max_bytes: int = DEFAULT_MAX_CREDENTIAL_FILE_BYTES,
) -> AsyncIterator[str]:
    """
    Yield a local path for a local file or deployment-owned storage object.

    Local paths are yielded unchanged and are never deleted.

    Supported remote values:
    - deployment-owned Azure HTTPS URLs
    - deployment-owned S3 HTTPS URLs
    - deployment-owned native s3:// URIs on S3 deployments

    Arbitrary remote URLs are intentionally rejected because this resolver is
    used for private keys and client certificates.
    """
    if not isinstance(path_or_url, str):
        raise TypeError("Credential path must be a string")

    # Preserve historical local-path behavior, including relative paths and
    # Windows drive paths. A value is considered remote only when it uses an
    # explicit URL form.
    if "://" not in path_or_url:
        yield path_or_url
        return

    temp_path = await download_storage_file_to_temp(
        ctx=ctx,
        path_or_url=path_or_url,
        suffix=suffix,
        max_bytes=max_bytes,
    )

    try:
        yield temp_path
    finally:
        try:
            Path(temp_path).unlink(missing_ok=True)
        except OSError:
            logger.bind(ctx=ctx).warning("Failed to remove temporary credential file")
