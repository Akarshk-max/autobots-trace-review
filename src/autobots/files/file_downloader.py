import json
import os
import tempfile
import time
import zipfile
from typing import Dict, List
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_types import AppTypes
from src.autobots.conn.aws.aws_s3 import get_aws_s3
from src.autobots.conn.object_storage.factory import get_public_storage, get_storage
from src.autobots.conn.object_storage.protocol import ObjectStorage
from src.autobots.data_model.context import Context
from src.autobots.files.file_converter import FileConverter
from src.autobots.files.file_mime_types import guess_extension_from_mime_type, normalize_mime_type
from src.autobots.files.user_files import UserFiles
from src.autobots.user.user_orm_model import UserORM


def _is_aws_storage_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme.lower() == "s3":
        return bool(parsed.netloc)

    if parsed.scheme.lower() != "https":
        return False

    host = parsed.netloc.lower()
    return (
        host == "s3.amazonaws.com"
        or (host.startswith("s3.") and host.endswith(".amazonaws.com"))
        or (".s3." in host and host.endswith(".amazonaws.com"))
    )


async def _get_deployment_storage_for_url(
    url: str,
) -> tuple[ObjectStorage | None, str | None]:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"https", "s3"}:
        return None, None

    for storage in (
        get_public_storage(object_prefix=""),
        get_storage(object_prefix=""),
    ):
        if parsed.scheme.lower() == "s3":
            if storage.provider == "s3" and parsed.netloc.lower() == storage.container_name.lower():
                key = await storage.get_key(url)
                return storage, key or None
            continue

        key = await storage.get_key_if_owned_url(url)
        if key is not None:
            return storage, key

    return None, None


class FileDownloader:
    @staticmethod
    async def _download_urls_to_tmpfiles(
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        urls: List[str],
        supported_file_formats: List[str] | None = None,
        cache: Dict[str, str] | None = None,  # url -> file_path # update cache in-place
    ) -> Dict[str, str]:  # url -> file_path
        """Download URLs to local temp files.

        Key goals:
        - Avoid loading full remote files into memory.
        - Preserve ORIGINAL input URLs as dictionary keys (even if request URLs are rewritten).
        - Clean up partial temp files on failures.
        """

        if cache is None:
            cache = {}

        result: Dict[str, str] = {}

        for url in urls:
            original_url = url

            # Cache hit: return cached path AND include in result.
            if original_url in cache:
                cached_path = cache.get(original_url)
                if cached_path and os.path.exists(cached_path):
                    result[original_url] = cached_path
                    continue
                # cache entry is stale (temp file deleted) -> re-download
                cache.pop(original_url, None)

            owned_tmp_paths: List[str] = []
            tmp_path: str | None = None

            try:
                url_t0 = time.perf_counter()

                download_method: str | None = None
                bytes_written: int = 0
                response_content_type: str | None = None

                file_name: str = await UserFiles.extract_file_name(ctx=ctx, file_url=original_url)
                file_ext: str = await UserFiles.extract_file_ext(ctx=ctx, file_url=original_url)

                # 1) Primary attempt: direct HTTP(S) streaming to temp file.
                if original_url.startswith("http://") or original_url.startswith("https://"):
                    try:
                        async with httpx.AsyncClient(follow_redirects=True) as http_client:
                            async with http_client.stream("GET", original_url) as response:
                                if response.status_code == 200:
                                    download_method = "http_primary"
                                    bytes_written = 0

                                    response_content_type = normalize_mime_type(
                                        response.headers.get("content-type")
                                    )
                                    if not file_ext:
                                        file_ext = guess_extension_from_mime_type(
                                            response_content_type
                                        )

                                    tmp = tempfile.NamedTemporaryFile(
                                        delete=False, suffix=file_ext or ""
                                    )
                                    owned_tmp_paths.append(tmp.name)
                                    try:
                                        async for chunk in response.aiter_bytes():
                                            bytes_written += len(chunk)
                                            tmp.write(chunk)
                                    finally:
                                        tmp.close()

                                    tmp_path = tmp.name
                                    logger.bind(ctx=ctx, tmpfile=tmp_path).debug(
                                        "written data to temp file"
                                    )
                                else:
                                    # Let fallback logic below handle errors.
                                    logger.bind(
                                        ctx=ctx, url=original_url, status_code=response.status_code
                                    ).warning("Primary HTTP download failed; will attempt fallback")
                    except Exception as e:
                        logger.bind(ctx=ctx, url=original_url).exception(
                            f"Error in downloading url through primary HTTP streaming: {str(e)}"
                        )

                # 2) Fallback: authenticated deployment storage, customer
                # AWS storage, or a secondary HTTP attempt.
                if tmp_path is None:
                    storage, storage_key = await _get_deployment_storage_for_url(original_url)

                    if storage is None and _is_aws_storage_url(original_url):
                        app_creds_doc = await UserAppCreds.get_app_creds_by_app_name(
                            ctx=ctx,
                            db=db,
                            user=user,
                            app_name=AppTypes.aws,
                            decrypt=True,
                        )
                        customer_storage = get_aws_s3(
                            region_name=app_creds_doc.creds["region_name"],
                            aws_access_key_id=app_creds_doc.creds["aws_access_key_id"],
                            aws_secret_access_key=app_creds_doc.creds["aws_secret_access_key"],
                            bucket_name=app_creds_doc.creds["bucket_name"],
                            object_prefix=app_creds_doc.creds["object_prefix"],
                        )

                        parsed_storage_url = urlparse(original_url)
                        if parsed_storage_url.scheme.lower() == "s3":
                            if (
                                parsed_storage_url.netloc.lower()
                                != customer_storage.container_name.lower()
                            ):
                                raise HTTPException(
                                    status_code=400,
                                    detail=(
                                        "S3 URL does not belong to the configured customer bucket"
                                    ),
                                )
                            storage_key = await customer_storage.get_key(original_url)
                        else:
                            storage_key = await customer_storage.get_key_if_owned_url(original_url)

                        if not storage_key:
                            raise HTTPException(
                                status_code=400,
                                detail=("AWS URL does not belong to configured object storage"),
                            )

                        storage = customer_storage

                    if storage is not None and storage_key:
                        logger.bind(
                            ctx=ctx,
                            provider=storage.provider,
                            container=storage.container_name,
                            key=storage_key,
                        ).info("Downloading through authenticated object storage")

                        download_method = (
                            "customer_s3"
                            if not storage.is_system_storage
                            else f"{storage.provider}_storage"
                        )
                        tmp = tempfile.NamedTemporaryFile(
                            delete=False,
                            suffix=file_ext or "",
                        )
                        owned_tmp_paths.append(tmp.name)
                        try:
                            await storage.download_key_to_fileobj(
                                ctx=ctx,
                                key=storage_key,
                                fileobj=tmp,
                            )
                        finally:
                            tmp.close()

                        tmp_path = tmp.name
                        bytes_written = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
                        logger.bind(
                            ctx=ctx,
                            tmpfile=tmp_path,
                        ).debug("Written object-storage data to temp file")

                    elif original_url.startswith(("http://", "https://")):
                        # Keep the original URL as the result key.
                        request_url = original_url

                        # Normalize Box links while preserving the result key.
                        if "box" in request_url and "?" in request_url:
                            base_url = request_url.split("?")[0]
                            redirect = request_url.split("?")[-1]
                            request_url = f"{base_url}?{redirect}"

                        async with httpx.AsyncClient(follow_redirects=True) as http_client:
                            async with http_client.stream(
                                "GET",
                                request_url,
                            ) as response:
                                if response.status_code != 200:
                                    logger.bind(
                                        ctx=ctx,
                                        url=original_url,
                                        request_url=request_url,
                                        status_code=response.status_code,
                                    ).error("Failed to download file in HTTP fallback")
                                    raise HTTPException(
                                        status_code=response.status_code,
                                        detail=(
                                            f"Failed to download file: HTTP {response.status_code}"
                                        ),
                                    )

                                download_method = "http_fallback"
                                bytes_written = 0

                                response_content_type = normalize_mime_type(
                                    response.headers.get("content-type")
                                )
                                if not file_ext:
                                    file_ext = guess_extension_from_mime_type(response_content_type)

                                tmp = tempfile.NamedTemporaryFile(
                                    delete=False,
                                    suffix=file_ext or "",
                                )
                                owned_tmp_paths.append(tmp.name)
                                try:
                                    async for chunk in response.aiter_bytes():
                                        bytes_written += len(chunk)
                                        tmp.write(chunk)
                                finally:
                                    tmp.close()

                                tmp_path = tmp.name
                                logger.bind(
                                    ctx=ctx,
                                    tmpfile=tmp_path,
                                ).debug("Written HTTP data to temp file")

                if tmp_path is None:
                    raise HTTPException(
                        status_code=500,
                        detail="Failed to download URL to a temporary file",
                    )

                # 3) If it's a .gsheet, parse JSON and re-download as CSV.
                # IMPORTANT: this must run regardless of download method.
                if file_ext == ".gsheet":
                    logger.bind(ctx=ctx, url=original_url).debug("Exporting .gsheet to CSV")

                    with open(tmp_path, "rb") as f:
                        meta = json.loads(f.read().decode("utf-8"))

                    sheet_url = meta.get("url")
                    if not sheet_url:
                        raise HTTPException(
                            status_code=400,
                            detail='Invalid .gsheet: missing "url" field',
                        )

                    export_url = sheet_url.rstrip("/edit") + "/export?format=csv"
                    export_t0 = time.perf_counter()
                    export_bytes_written = 0

                    csv_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
                    owned_tmp_paths.append(csv_tmp.name)
                    try:
                        async with httpx.AsyncClient(follow_redirects=True) as session:
                            async with session.stream("GET", export_url) as resp2:
                                if resp2.status_code != 200:
                                    raise HTTPException(
                                        status_code=resp2.status_code,
                                        detail=f"Failed to export Google Sheet: HTTP {resp2.status_code}",
                                    )

                                async for chunk in resp2.aiter_bytes():
                                    export_bytes_written += len(chunk)
                                    csv_tmp.write(chunk)
                    finally:
                        csv_tmp.close()

                    # Replace the original .gsheet meta file with the exported CSV
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    tmp_path = csv_tmp.name
                    file_ext = ".csv"
                    bytes_written = export_bytes_written

                    export_ms = int((time.perf_counter() - export_t0) * 1000)
                    logger.bind(
                        ctx=ctx,
                        url=original_url,
                        export_url=export_url,
                        csv_size_bytes=export_bytes_written,
                        export_ms=export_ms,
                    ).info("Exported .gsheet to CSV")

                # Log download summary
                download_ms = int((time.perf_counter() - url_t0) * 1000)
                final_size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else None
                if not download_method:
                    download_method = "unknown"
                logger.bind(
                    ctx=ctx,
                    url=original_url,
                    download_method=download_method,
                    download_ms=download_ms,
                    size_bytes=final_size,
                    bytes_written=bytes_written,
                    response_content_type=response_content_type,
                    file_ext=file_ext,
                ).info("Downloaded url to tmpfile")

                cache[original_url] = tmp_path
                result[original_url] = tmp_path

                # 4) Post-processing: zip or convert-to-txt if required by supported_file_formats.
                # supported_file_formats can be like [".pdf"], or it can be like ["pdf"] or a mix
                # file_ext is always of type ".pdf" (instead of "pdf")
                if (
                    supported_file_formats
                    and file_ext not in supported_file_formats
                    and file_ext[1:] not in supported_file_formats
                    and (".zip" in supported_file_formats or "zip" in supported_file_formats)
                ):
                    zip_t0 = time.perf_counter()
                    in_size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else None

                    tmp_zip = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
                    tmp_zip_path = tmp_zip.name
                    owned_tmp_paths.append(tmp_zip_path)
                    tmp_zip.close()
                    with zipfile.ZipFile(tmp_zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                        zipf.write(tmp_path, arcname=file_name)

                    zip_ms = int((time.perf_counter() - zip_t0) * 1000)
                    zip_size = (
                        os.path.getsize(tmp_zip_path) if os.path.exists(tmp_zip_path) else None
                    )
                    logger.bind(
                        ctx=ctx,
                        url=original_url,
                        in_size_bytes=in_size,
                        zip_size_bytes=zip_size,
                        zip_ms=zip_ms,
                    ).info("Zipped file to supported format")

                    # Replace original tmp_path with zip
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    tmp_path = tmp_zip_path
                    cache[original_url] = tmp_path
                    result[original_url] = tmp_path

                elif (
                    supported_file_formats
                    and file_ext not in supported_file_formats
                    and file_ext[1:] not in supported_file_formats
                ):
                    # Convert to text without holding the entire output in memory
                    convert_t0 = time.perf_counter()
                    in_size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else None

                    new_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
                    new_tmp_path = new_tmp.name
                    owned_tmp_paths.append(new_tmp_path)
                    new_tmp.close()

                    FileConverter._convert_file_to_text_file(
                        file_path=tmp_path,
                        out_path=new_tmp_path,
                    )

                    out_size = (
                        os.path.getsize(new_tmp_path) if os.path.exists(new_tmp_path) else None
                    )
                    convert_ms = int((time.perf_counter() - convert_t0) * 1000)
                    logger.bind(
                        ctx=ctx,
                        url=original_url,
                        in_size_bytes=in_size,
                        out_size_bytes=out_size,
                        convert_ms=convert_ms,
                    ).info("Converted file to txt for unsupported format")

                    # Replace original tmp_path with txt
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    tmp_path = new_tmp_path
                    cache[original_url] = tmp_path
                    result[original_url] = tmp_path

            except Exception as e:
                # Best-effort cleanup of any temp paths created for this URL.
                for p in owned_tmp_paths:
                    if p and os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            logger.bind(ctx=ctx, tmp_path=p, url=original_url).warning(
                                "Failed to remove temp file during cleanup"
                            )

                logger.bind(ctx=ctx, url=original_url).exception(
                    f"Error in downloading url to tmpfile: {str(e)}"
                )

        logger.bind(ctx=ctx, downloaded_urls=result).info("Downloaded urls to temp files")
        return result
