import asyncio
import os
import tempfile
from typing import BinaryIO, Callable, Iterable, Optional, Union
from urllib.parse import unquote, urlparse

import httpx
import xai_sdk
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_types import AppTypes
from src.autobots.conn.aws.aws_s3 import get_aws_s3, get_public_s3, get_s3
from src.autobots.conn.grok.grok_files.file_models import (
    DeleteResponse,
    FileMetadata,
    ListFilesResponse,
    XAIDeleteError,
    XAIDownloadError,
    XAIGetError,
    XAIListError,
    XAIUploadError,
)
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM

UploadSource = Union[str, bytes, BinaryIO]
ProgressCallback = Callable[[int, int], None]


class XAIFileManager:
    """
    This class manages how to handle files upload , delete , listing , fetching metadata,
    downloading content , fetching files etc.
    """

    def __init__(self) -> None:
        pass

    #  Upload a file simeple
    async def upload(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        source: UploadSource,
        *,
        filename: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> FileMetadata:
        """
        Upload a file from path, bytes, or file-like object.
        """
        try:
            file = await client.files.upload(
                source,
                filename=filename,
                on_progress=on_progress,
            )
            logger.bind(ctx=ctx, user=user).info(
                f"[GROK-FILE] File with file_id {file.id} is uploaded successfully"
            )
            return self._to_metadata(file)
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-FILE] File is failed to uploaded error is {exc}"
            )
            raise XAIUploadError("Failed to upload file") from exc

    # Batch upload used for uploading multiple files in batch at once
    async def batch_upload(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        paths: Iterable[str],
        *,
        batch_size: int = 3,
        on_file_complete: Callable[[int, str, object], None] | None = None,
    ) -> dict[str, FileMetadata]:
        """
        Upload multiple files concurrently with controlled concurrency.
        """
        try:
            results = await client.files.batch_upload(
                paths,
                batch_size=batch_size,
                on_file_complete=on_file_complete,
            )

            output: dict[str, FileMetadata] = {}
            for path, result in results.items():
                if isinstance(result, BaseException):
                    continue
                output[path] = self._to_metadata(result)
            logger.bind(ctx=ctx, user=user).info("[GROK-FILE] Files are uploaded successfully")
            return output
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-FILE] Files are failed to uploaded error is {exc}"
            )
            raise XAIUploadError("Batch upload failed") from exc

    # Listing down all the files uploaded to xai console
    async def list_files(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        *,
        limit: int = 10,
        order: str = "desc",
        sort_by: str = "created_at",
    ) -> ListFilesResponse:
        """
        List files with sorting and pagination support.
        """
        try:
            response = await client.files.list(
                limit=limit,
                order=order,
                sort_by=sort_by,
            )

            files = [self._to_metadata(f) for f in response.data]
            return ListFilesResponse(
                files=files,
                pagination_token=response.pagination_token,
            )
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-FILE] Listing files got error is {exc}"
            )
            raise XAIListError("Failed to list files") from exc

    # Fetching Metadata of a file_id
    async def get_metadata(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        file_id: str,
    ) -> FileMetadata:
        """
        Retrieve metadata for a single file.
        """
        try:
            file = await client.files.get(file_id)
            return self._to_metadata(file)
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-FILE] Failed to get metadata of file {exc}"
            )
            raise XAIGetError(f"Failed to get metadata for file {file_id}") from exc

    # Download content of a file by providing it's file_id
    async def download_content(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        file_id: str,
    ) -> bytes:
        """
        Download raw file content.
        """
        try:
            return await client.files.content(file_id)
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-FILE] Failed to download content, error is {exc}"
            )
            raise XAIDownloadError(f"Failed to download file {file_id}") from exc

    # Downloading the files to specific file path
    async def download_to_path(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        file_id: str,
        path: str,
    ) -> None:
        """
        Download file content directly to disk.
        """
        content = await self.download_content(
            ctx=ctx, user=user, db=db, client=client, file_id=file_id
        )
        try:
            with open(path, "wb") as f:
                f.write(content)
        except OSError as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-FILE] Failed to write file error is {exc}"
            )
            raise XAIDownloadError(f"Failed to write file to {path}") from exc

    #  Deletion of uploaded files by providing file_id
    async def delete(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        file_id: str,
    ) -> DeleteResponse:
        """
        Delete a file by ID.
        """
        try:
            response = await client.files.delete(file_id)
            logger.bind(ctx=ctx, user=user).info("[GROK-FILE] File is deleted successfully")
            return DeleteResponse(id=response.id, deleted=response.deleted)
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-FILE] Failed to delete file , error is {exc}"
            )
            raise XAIDeleteError(f"Failed to delete file {file_id}") from exc

    # Upload files to the xai console by giving urls
    async def upload_urls(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        urls: list[str],
        *,
        batch_size: int = 3,
        on_file_complete: Callable[[int, str, object], None] | None = None,
        on_progress: ProgressCallback | None = None,
        timeout_sec: float = 500.0,
    ) -> dict[str, FileMetadata]:
        """
        Download from s3:// or https:// URLs to disk (streaming) and upload to xAI.
        Rejects http:// URLs. http urls will be blocked for now
        """
        sem = asyncio.Semaphore(batch_size)
        results: dict[str, object] = {}

        async def _one(idx: int, url: str) -> None:
            async with sem:
                try:
                    tmp_path, inferred_filename = await self._download_url_to_tempfile(
                        ctx=ctx,
                        user=user,
                        db=db,
                        url=url,
                        on_progress=on_progress,
                        timeout_sec=timeout_sec,
                    )
                    try:
                        meta = await self.upload(
                            ctx=ctx,
                            user=user,
                            db=db,
                            client=client,
                            source=tmp_path,
                            filename=inferred_filename,
                            on_progress=on_progress,
                        )
                        results[url] = meta
                        if on_file_complete:
                            on_file_complete(idx, url, meta)
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                except Exception as exc:
                    results[url] = exc
                    if on_file_complete:
                        on_file_complete(idx, url, exc)

        tasks = [asyncio.create_task(_one(i, u)) for i, u in enumerate(urls)]
        await asyncio.gather(*tasks)

        output: dict[str, FileMetadata] = {}
        for url, res in results.items():
            if isinstance(res, BaseException):
                continue
            output[url] = res  # type: ignore[assignment]
        return output

    async def _download_url_to_tempfile(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        url: str,
        *,
        on_progress: ProgressCallback | None,
        timeout_sec: float,
    ) -> tuple[str, Optional[str]]:
        parsed = urlparse(url)

        if parsed.scheme == "http":
            raise XAIDownloadError("http URLs are not allowed. Use https:// or s3:// only.")

        if parsed.scheme == "https":
            return await self._download_https_to_tempfile(
                ctx=ctx,
                user=user,
                db=db,
                url=url,
                on_progress=on_progress,
                timeout_sec=timeout_sec,
            )

        if parsed.scheme == "s3":
            return await self._download_s3_to_tempfile(
                ctx=ctx,
                user=user,
                db=db,
                url=url,
                on_progress=on_progress,
            )

        raise XAIDownloadError(
            f"Unsupported URL scheme '{parsed.scheme}'. Only https:// and s3:// are supported."
        )

    # Download the https url to the tmp file
    async def _download_https_to_tempfile(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        url: str,
        *,
        on_progress: ProgressCallback | None,
        timeout_sec: float,
    ) -> tuple[str, Optional[str]]:
        parsed = urlparse(url)
        filename = os.path.basename(parsed.path) or None
        filename = unquote(filename) if filename else None

        tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=".bin", delete=False)
        tmp_path = tmp.name
        tmp.close()

        bytes_read = 0
        total = 0

        try:
            timeout = httpx.Timeout(timeout_sec, connect=30.0)
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as h:
                async with h.stream("GET", url) as r:
                    r.raise_for_status()

                    final_url = str(r.url)
                    final_parsed = urlparse(final_url)
                    if final_parsed.scheme == "http":
                        raise XAIDownloadError("Redirected to http URL which is not allowed.")

                    # Best-effort total size as mentioned in xai documentation
                    # https://docs.x.ai/docs/guides/files/managing-files
                    cl = r.headers.get("content-length")
                    if cl and cl.isdigit():
                        total = int(cl)

                    with open(tmp_path, "wb") as f:
                        async for chunk in r.aiter_bytes(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            f.write(chunk)
                            bytes_read += len(chunk)
                            if on_progress:
                                on_progress(bytes_read, total)

            # If server didn't send content-length, report final total
            if on_progress and total == 0:
                on_progress(bytes_read, bytes_read)

            return tmp_path, filename
        except Exception as exc:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-FILE] Failed to download https url, error is {exc}"
            )
            raise XAIDownloadError(f"Failed to download from {url}") from exc

    # Download the s3 url content to a tmp file
    async def _download_s3_to_tempfile(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        url: str,
        *,
        on_progress: ProgressCallback | None,
    ) -> tuple[str, Optional[str]]:
        parsed = urlparse(url)
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")

        filename = os.path.basename(key) or None
        filename = unquote(filename) if filename else None

        tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=".bin", delete=False)
        tmp_path = tmp.name
        tmp.close()

        try:
            s3 = None

            # internal private bucket
            if bucket == SettingsProvider.sget().AWS_S3_BUCKET_NAME:
                s3 = get_s3()

            # public bucket
            elif bucket == SettingsProvider.sget().AWS_S3_PUBLIC_BUCKET_NAME:
                s3 = get_public_s3()

            # user-provided AWS credentials
            else:
                app_creds = await UserAppCreds.get_app_creds_by_app_name(
                    ctx=ctx,
                    db=db,
                    user=user,
                    app_name=AppTypes.aws,
                    decrypt=True,
                )

                s3 = get_aws_s3(
                    region_name=app_creds.creds["region_name"],
                    aws_access_key_id=app_creds.creds["aws_access_key_id"],
                    aws_secret_access_key=app_creds.creds["aws_secret_access_key"],
                    bucket_name=app_creds.creds["bucket_name"],
                    object_prefix=app_creds.creds["object_prefix"],
                )

            assert s3 is not None

            bytes_written = 0

            def _download_blocking():
                nonlocal bytes_written
                with open(tmp_path, "wb") as f:
                    for chunk in s3.iter_bytes(key=key, chunk_size=1024 * 1024):
                        f.write(chunk)
                        bytes_written += len(chunk)
                        if on_progress:
                            on_progress(bytes_written, 0)

            await asyncio.to_thread(_download_blocking)

            if on_progress:
                on_progress(bytes_written, bytes_written)

            return tmp_path, filename

        except Exception as exc:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-FILE] Failed to download s3 url {url}, error is {exc}"
            )
            raise XAIDownloadError(f"Failed to download from {url}") from exc

    #  Internal helpers method
    @staticmethod
    def _to_metadata(file: object) -> FileMetadata:
        """
        Convert SDK file object to pydantic model.
        """
        return FileMetadata(
            id=file.id,
            filename=file.filename,
            size=file.size,
            team_id=getattr(file, "team_id", None),
            created_at=file.created_at.ToDatetime(),
            expires_at=file.expires_at.ToDatetime() if file.HasField("expires_at") else None,
        )
