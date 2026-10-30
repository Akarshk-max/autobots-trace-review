# src/autobots/ingestion/url_import_utils.py

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from loguru import logger

from src.autobots.conn.object_storage.protocol import ObjectStorage
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.user.user_orm_model import UserORM

DEFAULT_MAX_REMOTE_BYTES = 200 * 1024 * 1024  # 200MB


def extract_key_if_url_is_in_bucket(url: str, bucket_name: str) -> Optional[str]:
    """
    Returns the S3 object key iff the URL points to the given bucket.
    Supports:
      - virtual-hosted: https://{bucket}.s3.amazonaws.com/{key}
      - virtual-hosted: https://{bucket}.s3.<region>.amazonaws.com/{key}
      - path-style:     https://s3.amazonaws.com/{bucket}/{key}
      - path-style:     https://s3.<region>.amazonaws.com/{bucket}/{key}

    If the URL is not in that bucket (including other S3 buckets), returns None.
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lstrip("/")
    bucket = (bucket_name or "").lower()

    # virtual-hosted style
    if host.startswith(f"{bucket}.") and "amazonaws.com" in host:
        return path or None

    # path-style
    is_path_style = host == "s3.amazonaws.com" or (
        host.startswith("s3.") and host.endswith(".amazonaws.com")
    )
    if is_path_style and path:
        parts = path.split("/", 1)
        if len(parts) == 2 and parts[0].lower() == bucket:
            return parts[1] or None

    return None


async def _download_url_to_tmpfile(
    url: str,
    suffix: str,
    max_size_bytes: int = DEFAULT_MAX_REMOTE_BYTES,
) -> Path:
    """
    Stream-download a URL to a temp file with a hard size limit.
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp_path = Path(tmp.name)
    tmp.close()

    timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=30.0)
    downloaded = 0

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            # Best-effort HEAD guard (not all servers support HEAD)
            try:
                head = await client.head(url)
                if head.status_code < 400:
                    cl = head.headers.get("content-length")
                    if cl and int(cl) > max_size_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"Remote file exceeds size limit ({max_size_bytes} bytes).",
                        )
            except HTTPException:
                raise
            except Exception:
                pass

            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > max_size_bytes:
                            raise HTTPException(
                                status_code=413,
                                detail=f"Remote file exceeds size limit ({max_size_bytes} bytes).",
                            )
                        f.write(chunk)
        return tmp_path
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class ImportedURLForKB:
    original_filename: str
    persisted_s3_key: str
    persisted_s3_url: str
    staging_s3_key: str


@dataclass(frozen=True)
class ImportedURLForSandbox:
    original_filename: str
    s3_key: str
    s3_url: str


async def import_public_url_to_public_s3_and_stage_for_kb(
    ctx: Context,
    user: UserORM,
    public_s3: ObjectStorage,
    remote_url: str,
    job_id: str,
    original_filename: Optional[str] = None,
    max_size_bytes: int = DEFAULT_MAX_REMOTE_BYTES,
) -> ImportedURLForKB:
    """
    For KB ingestion:
      - download public URL
      - persist to permanent user folder in our public bucket (files/<user_id>/...)
      - copy to staging/<job_id>/... for worker isolation + cleanup
    """
    # derive display filename if missing
    display_name = original_filename or await UserFiles.extract_file_name(
        ctx=ctx, file_url=remote_url
    )

    user_files = UserFiles(user=user, s3=public_s3)
    secure_display_name = await user_files.secure_filename(ctx, display_name)
    if not secure_display_name:
        raise HTTPException(status_code=400, detail="Could not determine a valid filename.")

    suffix = Path(secure_display_name).suffix
    if not suffix:
        raise HTTPException(status_code=400, detail="Filename must include an extension.")

    tmp_path: Optional[Path] = None
    try:
        tmp_path = await _download_url_to_tmpfile(
            remote_url, suffix=suffix, max_size_bytes=max_size_bytes
        )

        # Prefix with job_id to avoid collisions in permanent user folder
        persisted_filename = f"{job_id}_{secure_display_name}"

        with open(tmp_path, "rb") as f:
            metas = await user_files.upload_binary(ctx=ctx, file_obj=f, filename=persisted_filename)
        if not metas:
            raise HTTPException(status_code=500, detail="Failed to upload imported URL to S3.")

        persisted_meta = metas[0]
        persisted_s3_url = str(persisted_meta.url)
        persisted_s3_key = await public_s3.get_key_from_https_url(persisted_s3_url)

        staged_filename = persisted_s3_key.split("/")[-1]
        staging_s3_key = f"staging/{job_id}/{staged_filename}"

        await public_s3.copy(
            source_key=persisted_s3_key,
            destination_key=staging_s3_key,
        )

        logger.bind(ctx=ctx).info(
            f"Imported URL persisted_key='{persisted_s3_key}', staged_key='{staging_s3_key}'"
        )

        return ImportedURLForKB(
            original_filename=secure_display_name,
            persisted_s3_key=persisted_s3_key,
            persisted_s3_url=persisted_s3_url,
            staging_s3_key=staging_s3_key,
        )
    finally:
        if tmp_path:
            tmp_path.unlink(missing_ok=True)


async def import_public_url_to_public_s3_for_sandbox(
    ctx: Context,
    user: UserORM,
    public_s3: ObjectStorage,
    remote_url: str,
    original_filename: Optional[str] = None,
    max_size_bytes: int = DEFAULT_MAX_REMOTE_BYTES,
) -> ImportedURLForSandbox:
    """
    For sandbox ingestion:
      - download public URL
      - upload into sandbox/<user_id>/... in our public bucket
      - return the key; caller is responsible for deleting it after use
    """
    display_name = original_filename or await UserFiles.extract_file_name(
        ctx=ctx, file_url=remote_url
    )

    sandbox_files = UserFiles(user=user, s3=public_s3, top_level_folder="sandbox")
    secure_display_name = await sandbox_files.secure_filename(ctx, display_name)
    if not secure_display_name:
        raise HTTPException(status_code=400, detail="Could not determine a valid filename.")

    suffix = Path(secure_display_name).suffix
    if not suffix:
        raise HTTPException(status_code=400, detail="Filename must include an extension.")

    tmp_path: Optional[Path] = None
    try:
        tmp_path = await _download_url_to_tmpfile(
            remote_url, suffix=suffix, max_size_bytes=max_size_bytes
        )

        with open(tmp_path, "rb") as f:
            metas = await sandbox_files.upload_binary(
                ctx=ctx, file_obj=f, filename=secure_display_name
            )
        if not metas:
            raise HTTPException(status_code=500, detail="Failed to upload sandbox URL to S3.")

        persisted_url = str(metas[0].url)
        s3_key = await public_s3.get_key_from_https_url(persisted_url)

        return ImportedURLForSandbox(
            original_filename=secure_display_name,
            s3_key=s3_key,
            s3_url=persisted_url,
        )
    finally:
        if tmp_path:
            tmp_path.unlink(missing_ok=True)
