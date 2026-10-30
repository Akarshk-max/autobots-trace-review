import mimetypes
import os
import tempfile
from typing import Dict, List

from anthropic.types.beta import BetaFileMetadata
from google.genai import types
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai.types import FileObject

from src.autobots.conn.claude.claude_client import Claude
from src.autobots.conn.gemini.gemini import GeminiClient
from src.autobots.conn.openai.openai_client import OpenAI
from src.autobots.conn.openai.openai_files.openai_files_model import FileCreate
from src.autobots.data_model.context import Context
from src.autobots.files.file_converter import FileConverter
from src.autobots.files.file_downloader import FileDownloader
from src.autobots.files.file_mime_types import is_generic_binary_mime_type, resolve_file_mime_type
from src.autobots.files.user_files import UserFiles
from src.autobots.user.user_orm_model import UserORM

# OpenAI file size limit is 512 MB, using 510 MB as safe threshold
MAX_OPENAI_FILE_SIZE_BYTES = 510 * 1024 * 1024


class FileUploader:
    @staticmethod
    async def upload_urls_to_openai(
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        urls: List[str],
        openai_client: OpenAI,
        supported_file_formats: List[str],
        cache: Dict[str, FileObject] | None = None,  # update in-place
    ) -> Dict[str, FileObject]:
        if cache is None:
            cache = {}
        urls_to_tmpfiles: Dict[str, str] = {}
        try:
            new_urls = [url for url in urls if url not in cache]
            # supported_file_formats: List[str] = OPENAI_FILE_FORMATS
            urls_to_tmpfiles: Dict[str, str] = await FileDownloader._download_urls_to_tmpfiles(
                ctx=ctx,
                user=user,
                db=db,
                urls=new_urls,
                supported_file_formats=supported_file_formats,
            )
            logger.bind(ctx=ctx, downloaded_urls=urls_to_tmpfiles).info(
                "Downloaded urls to temp files"
            )
            urls_to_openai_file_obj: Dict[
                str, FileObject
            ] = await FileUploader._upload_files_to_openai(
                ctx=ctx,
                user=user,
                db=db,
                file_urls_to_paths=urls_to_tmpfiles,
                openai_client=openai_client,
                cache=cache,
            )
            logger.bind(ctx=ctx, uploaded_urls=urls_to_openai_file_obj).info(
                "Uploaded urls to openai"
            )
            return urls_to_openai_file_obj
        finally:
            for url, tmpfile in urls_to_tmpfiles.items():
                if os.path.exists(tmpfile):
                    os.remove(tmpfile)
                    logger.bind(ctx=ctx, url=url).info("Removed temp file")

    @staticmethod
    async def upload_urls_to_gemini(
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        urls: List[str],
        gemini_client: GeminiClient,
        supported_file_formats: List[str] | None = None,
        cache: Dict[str, types.File] | None = None,  # update in-place
    ) -> Dict[str, types.File]:
        if cache is None:
            cache = {}
        urls_to_tmpfiles: Dict[str, str] = {}
        try:
            new_urls = [url for url in urls if url not in cache]
            # supported_file_formats: List[str] = OPENAI_FILE_FORMATS
            urls_to_tmpfiles: Dict[str, str] = await FileDownloader._download_urls_to_tmpfiles(
                ctx=ctx,
                user=user,
                db=db,
                urls=new_urls,
                supported_file_formats=supported_file_formats,
            )
            logger.bind(ctx=ctx, downloaded_urls=urls_to_tmpfiles).info(
                "Downloaded urls to temp files"
            )
            urls_to_gemini_file: Dict[str, types.File] = await FileUploader._upload_files_to_gemini(
                ctx=ctx,
                user=user,
                db=db,
                file_urls_to_paths=urls_to_tmpfiles,
                gemini_client=gemini_client,
                cache=cache,
            )
            logger.bind(ctx=ctx, uploaded_urls=urls_to_gemini_file).info("Uploaded urls to gemini")
            return urls_to_gemini_file
        finally:
            for url, tmpfile in urls_to_tmpfiles.items():
                if os.path.exists(tmpfile):
                    os.remove(tmpfile)
                    logger.bind(ctx=ctx, url=url).info("Removed temp file")

    @staticmethod
    async def upload_urls_to_claude(
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        urls: List[str],
        claude_client: Claude,
        supported_file_formats: List[str] | None = None,
        cache: Dict[str, BetaFileMetadata] | None = None,
    ) -> Dict[str, BetaFileMetadata]:
        if cache is None:
            cache = {}

        urls_to_tmpfiles: Dict[str, str] = {}
        result: Dict[str, BetaFileMetadata] = {url: cache[url] for url in urls if url in cache}

        try:
            new_urls = [url for url in urls if url not in cache]
            urls_to_tmpfiles = await FileDownloader._download_urls_to_tmpfiles(
                ctx=ctx,
                user=user,
                db=db,
                urls=new_urls,
                supported_file_formats=supported_file_formats,
            )
            logger.bind(ctx=ctx, downloaded_urls=urls_to_tmpfiles).info(
                "Downloaded urls to temp files for Claude"
            )

            uploaded = await FileUploader._upload_files_to_claude(
                ctx=ctx,
                user=user,
                db=db,
                file_urls_to_paths=urls_to_tmpfiles,
                claude_client=claude_client,
                cache=cache,
            )
            result.update(uploaded)

            logger.bind(
                ctx=ctx,
                uploaded_urls={url: file_obj.id for url, file_obj in result.items()},
            ).info("Uploaded urls to Claude")
            return result
        finally:
            for url, tmpfile in urls_to_tmpfiles.items():
                if os.path.exists(tmpfile):
                    os.remove(tmpfile)
                    logger.bind(ctx=ctx, url=url).info("Removed temp file")


    @staticmethod
    async def _upload_files_to_claude(
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        file_urls_to_paths: Dict[str, str],
        claude_client: Claude,
        cache: Dict[str, BetaFileMetadata] | None = None,
    ) -> Dict[str, BetaFileMetadata]:
        if cache is None:
            cache = {}

        result: Dict[str, BetaFileMetadata] = {}
        client = claude_client.client

        for url, path in file_urls_to_paths.items():
            if url in cache:
                result[url] = cache[url]
                continue

            try:
                path_ext = await UserFiles.extract_file_ext(ctx=ctx, file_url=path)
                url_ext = await UserFiles.extract_file_ext(ctx=ctx, file_url=url)
                file_name: str = await UserFiles.extract_file_name(ctx=ctx, file_url=url)

                if path_ext != url_ext:
                    file_name = file_name.replace(".", "_") + path_ext

                mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"

                with open(path, "rb") as file_handle:
                    file_object: BetaFileMetadata = await client.beta.files.upload(
                        file=(file_name, file_handle, mime_type)
                    )

                cache[url] = file_object
                result[url] = file_object

                logger.bind(
                    ctx=ctx,
                    url=url,
                    file_id=file_object.id,
                    filename=getattr(file_object, "filename", file_name),
                ).info("File uploaded successfully to Claude")

            except Exception as e:
                logger.bind(ctx=ctx, url=url, path=path).exception(
                    f"Error in uploading tmpfiles to Claude: {str(e)}"
                )

        logger.bind(
            ctx=ctx,
            files={url: file_obj.id for url, file_obj in result.items()},
        ).info("Uploaded urls to Claude")

        return result
    
    # @staticmethod
    # async def _download_urls_to_tmpfiles(
    #     ctx: Context,
    #     user: UserORM,
    #     db: AsyncIOMotorDatabase,
    #     urls: List[str],
    #     supported_file_formats: List[str] | None = None,
    #     cache: Dict[str, str] | None = None,  # url -> file_path # update cache in-place
    # ) -> Dict[str, str]:  # url -> file_path
    #     if cache is None:
    #         cache = {}
    #     result: Dict[str, str] = {}
    #     for url in urls:
    #         if url in cache:
    #             continue
    #         try:
    #             file_bytes: bytes | None = None
    #             file_name: str = await UserFiles.extract_file_name(ctx=ctx, file_url=url)
    #             file_ext: str = await UserFiles.extract_file_ext(ctx=ctx, file_url=url)
    #             is_able_to_download = False
    #             try:  # try default
    #                 async with httpx.AsyncClient(follow_redirects=True) as http_client:
    #                     response = await http_client.get(url)
    #                     if response.status_code != 200:
    #                         logger.bind(ctx=ctx, url=url).error(
    #                             f"Error in getting file from url using httpx: {url}"
    #                         )
    #                     else:
    #                         file_bytes = response.content
    #                         is_able_to_download = True
    #
    #             except Exception as e:
    #                 logger.bind(ctx=ctx, url=url).exception(f"Error in downloading url through default: {str(e)}")
    #
    #             if not is_able_to_download:
    #                 if url.__contains__(".s3.") or url.startswith("s3://"):
    #                     s3_key = ""
    #                     s3: AwsS3 | None = None
    #                     if url.__contains__(SettingsProvider.sget().AWS_S3_BUCKET_NAME):
    #                         s3 = get_s3()
    #                     elif url.__contains__(SettingsProvider.sget().AWS_S3_PUBLIC_BUCKET_NAME):
    #                         s3 = get_public_s3()
    #                     else:
    #                         app_creds_doc = await UserAppCreds.get_app_creds_by_app_name(
    #                             ctx=Context(), db=db, user=user, app_name=AppTypes.aws
    #                         )
    #                         user_bucket_name = app_creds_doc.creds["bucket_name"]
    #                         assert user_bucket_name in url
    #                         s3 = get_aws_s3(
    #                             region_name=app_creds_doc.creds["region_name"],
    #                             aws_access_key_id=app_creds_doc.creds["aws_access_key_id"],
    #                             aws_secret_access_key=app_creds_doc.creds["aws_secret_access_key"],
    #                             bucket_name=app_creds_doc.creds["bucket_name"],
    #                             object_prefix=app_creds_doc.creds["object_prefix"],
    #                         )
    #                     assert s3 is not None
    #                     if url.__contains__(".s3."):
    #                         s3_key = await s3.get_key_from_https_url(url=url)
    #                     if url.startswith("s3://"):
    #                         s3_key = await s3.get_key_from_s3_uri(url=url)
    #                     assert s3_key
    #                     logger.bind(ctx=ctx, s3_key=s3_key).info(f"got s3 key: {s3_key}")
    #                     file_bytes = await s3.get_bytes(ctx=ctx, key=s3_key)
    #                 elif url.startswith("http://") or url.startswith("https://"):
    #                     # special handling for Box links
    #                     if "box" in url and "?" in url:
    #                         base_url = url.split("?")[0]
    #                         redirect = url.split("?")[-1]
    #                         url = f"{base_url}?{redirect}"
    #                     async with httpx.AsyncClient(follow_redirects=True) as http_client:
    #                         response = await http_client.get(url)
    #                         if response.status_code != 200:
    #                             logger.bind(ctx=ctx, url=url).error(
    #                                 f"openai_responses.upload_urls_to_openai: Error in getting file from url: {url}"
    #                             )
    #                             assert response.status_code == 200
    #                         file_bytes = response.content
    #                 assert file_bytes
    #
    #                 # 2. if it's a .gsheet, parse JSON and re-download as CSV
    #                 if file_ext == ".gsheet":
    #                     logger.bind(ctx=ctx).debug("downloading for google sheet sheet")
    #                     meta = json.loads(file_bytes.decode("utf-8"))
    #                     sheet_url = meta.get("url")
    #                     if not sheet_url:
    #                         raise HTTPException(
    #                             status_code=400, detail='Invalid .gsheet: missing "url" field'
    #                         )
    #                     export_url = sheet_url.rstrip("/edit") + "/export?format=csv"
    #                     async with httpx.AsyncClient(follow_redirects=True) as session:
    #                         resp2 = await session.get(export_url)
    #                         if resp2.status_code != 200:
    #                             raise HTTPException(
    #                                 status_code=resp2.status_code,
    #                                 detail=f"Failed to export Google Sheet: HTTP {resp2.status_code}",
    #                             )
    #                         file_bytes = resp2.content
    #                     file_ext = ".csv"
    #
    #             tmp = tempfile.NamedTemporaryFile(delete=False, suffix=file_ext)
    #             tmp.write(file_bytes)
    #             logger.bind(ctx=ctx, tmpfile=tmp).debug("written data to temp file")
    #             tmp.close()
    #
    #             cache[url] = tmp.name
    #             result[url] = tmp.name
    #
    #             # supported_file_formats can be like [".pdf"], or it can be like ["pdf"] or a mix
    #             # file_ext is always of type ".pdf" (instead of "pdf")
    #             if (
    #                     supported_file_formats
    #                     and file_ext not in supported_file_formats
    #                     and file_ext[1:] not in supported_file_formats
    #                     and (".zip" in supported_file_formats or "zip" in supported_file_formats)
    #             ):
    #                 tmp_zip = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    #                 with zipfile.ZipFile(tmp_zip.name, "w", zipfile.ZIP_DEFLATED) as zipf:
    #                     zipf.write(tmp.name, arcname=file_name)
    #                     logger.bind(ctx=ctx, url=url, tmpfile=tmp_zip).info(
    #                         "zipped data to temp file"
    #                     )
    #                 tmp_zip.close()
    #
    #                 cache[url] = tmp_zip.name
    #                 result[url] = tmp_zip.name
    #                 # remove the original file as no longer needed
    #                 if os.path.exists(tmp.name):
    #                     os.remove(tmp.name)
    #
    #             elif (
    #                     supported_file_formats
    #                     and file_ext not in supported_file_formats
    #                     and file_ext[1:] not in supported_file_formats
    #             ):
    #                 file_str = FileConverter._convert_file_to_text(file_path=tmp.name)
    #                 new_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    #                 new_tmp.write(file_str.encode("utf-8"))
    #                 logger.bind(ctx=ctx, url=url, tmpfile=new_tmp.name).debug(
    #                     "written data to temp file txt"
    #                 )
    #                 new_tmp.close()
    #
    #                 cache[url] = new_tmp.name
    #                 result[url] = new_tmp.name
    #                 # remove the original file as no longer needed
    #                 if os.path.exists(tmp.name):
    #                     os.remove(tmp.name)
    #
    #         except Exception as e:
    #             logger.bind(ctx=ctx, url=url).exception(
    #                 f"Error in downloading url to tmpfile: {str(e)}"
    #             )
    #
    #     logger.bind(ctx=ctx, downloaded_urls=result).info("Downloaded urls to temp files")
    #     return result

    @staticmethod
    async def _upload_files_to_openai(
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        file_urls_to_paths: Dict[str, str],
        openai_client: OpenAI,
        cache: Dict[str, FileObject] | None = None,  # update cache in-place
    ) -> Dict[str, FileObject]:
        if cache is None:
            cache = {}
        result: Dict[str, FileObject] = {}
        openai_files = openai_client.openai_files
        for url, path in file_urls_to_paths.items():
            if url in cache:
                continue
            # Skip files larger than 510MB
            file_size = os.path.getsize(path)
            if file_size > MAX_OPENAI_FILE_SIZE_BYTES:
                logger.bind(
                    ctx=ctx,
                    url=url,
                    file_size_mb=round(file_size / (1024 * 1024), 2),
                ).warning("Skipping file upload to OpenAI: file exceeds 510MB limit")
                continue
            # Skip empty files (0 bytes) - OpenAI rejects them with "File is empty" error, this was important
            if file_size == 0:
                logger.bind(ctx=ctx, url=url).warning(
                    "Skipping file upload to OpenAI: file is empty (0 bytes)"
                )
                continue
            try:
                path_ext = await UserFiles.extract_file_ext(ctx=ctx, file_url=path)
                url_ext = await UserFiles.extract_file_ext(ctx=ctx, file_url=url)
                file_name: str = await UserFiles.extract_file_name(ctx=ctx, file_url=url)
                if path_ext != url_ext:
                    file_name = file_name.replace(".", "_")
                    file_name = file_name + path_ext

                file_bytes = None
                with open(path, "rb") as file:
                    file_bytes = file.read()

                file_tuple = (file_name, file_bytes)
                file_create = FileCreate(
                    file=file_tuple,
                    purpose="assistants",
                )
                file_object: FileObject = await openai_files.create(
                    ctx=ctx, file_create=file_create
                )
                cache[url] = file_object
                result[url] = file_object
            except Exception as e:
                logger.bind(ctx=ctx, url=url, path=path).exception(
                    f"Error in uploading tmpfiles to openai: {str(e)}"
                )
        logger.bind(ctx=ctx, files=result).info("Uploaded urls to openai")
        return result

    @staticmethod
    def _looks_like_meaningful_text(content: str) -> bool:
        if not content:
            return False
        printable = sum(1 for ch in content if ch.isprintable() or ch in "\n\r\t")
        ratio = printable / max(len(content), 1)
        return ratio >= 0.85 and bool(content.strip())

    @classmethod
    def _try_prepare_text_fallback_for_gemini(
        cls,
        *,
        ctx: Context,
        path: str,
        file_name: str,
        source_url: str,
    ) -> tuple[str, str, str] | None:
        text_content: str | None = None
        suffix = os.path.splitext(file_name)[1].lower() or os.path.splitext(path)[1].lower()

        try:
            if suffix in set(FileConverter.SUPPORTED_FILE_TYPES):
                text_content = FileConverter._convert_file_to_text(file_path=path)
            elif suffix in {
                ".txt",
                ".md",
                ".markdown",
                ".csv",
                ".tsv",
                ".json",
                ".jsonl",
                ".xml",
                ".html",
                ".htm",
                "",
            }:
                for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
                    try:
                        with open(path, "r", encoding=encoding) as handle:
                            candidate = handle.read()
                    except Exception:
                        continue
                    if cls._looks_like_meaningful_text(candidate):
                        text_content = candidate
                        break
        except Exception as exc:
            logger.bind(
                ctx=ctx,
                source_url=source_url,
                path=path,
                file_name=file_name,
            ).warning("Failed Gemini text fallback conversion attempt: {}", exc)
            text_content = None

        if not text_content or not cls._looks_like_meaningful_text(text_content):
            return None

        base_name = os.path.splitext(file_name)[0] or "converted_file"
        fallback_name = f"{base_name}.txt"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        with open(tmp.name, "wb") as handle:
            handle.write(text_content.encode("utf-8"))

        logger.bind(
            ctx=ctx,
            source_url=source_url,
            original_file_name=file_name,
            fallback_file_name=fallback_name,
        ).info("Converted unresolved Gemini file input to text/plain fallback")

        return tmp.name, fallback_name, "text/plain"

    @classmethod
    async def _upload_files_to_gemini(
        cls,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        file_urls_to_paths: Dict[str, str],
        gemini_client: GeminiClient,
        wait_for_completion: bool = True,
        cache: Dict[str, types.File] | None = None,  # update cache in-place
    ) -> Dict[str, types.File]:
        if cache is None:
            cache = {}
        result: Dict[str, types.File] = {}
        converted_tmp_paths: list[str] = []
        try:
            for url, path in file_urls_to_paths.items():
                if url in cache:
                    continue
                try:
                    path_ext = await UserFiles.extract_file_ext(ctx=ctx, file_url=path)
                    url_ext = await UserFiles.extract_file_ext(ctx=ctx, file_url=url)
                    file_name: str = await UserFiles.extract_file_name(ctx=ctx, file_url=url)
                    if path_ext != url_ext and path_ext:
                        file_name = file_name.replace(".", "_")
                        file_name = file_name + path_ext

                    upload_path = path
                    upload_file_name = file_name
                    mime_type = resolve_file_mime_type(
                        file_name=file_name,
                        path=path,
                        source_url=url,
                    )

                    if not mime_type or is_generic_binary_mime_type(mime_type):
                        fallback_upload = cls._try_prepare_text_fallback_for_gemini(
                            ctx=ctx,
                            path=path,
                            file_name=file_name,
                            source_url=url,
                        )
                        if fallback_upload is not None:
                            upload_path, upload_file_name, mime_type = fallback_upload
                            converted_tmp_paths.append(upload_path)
                    file_object: types.File = await gemini_client.upload_file(
                        ctx=ctx,
                        path=upload_path,
                        wait_for_completion=wait_for_completion,
                        mime_type=mime_type,
                        display_name=upload_file_name,
                    )
                    cache[url] = file_object
                    result[url] = file_object
                except Exception as e:
                    logger.bind(ctx=ctx, url=url, path=path).exception(
                        f"Error in uploading tmpfiles to gemini: {str(e)}"
                    )
            logger.bind(ctx=ctx, files=result).info("Uploaded urls to gemini")
            return result
        finally:
            for tmp_path in converted_tmp_paths:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

    # @staticmethod
    # async def upload_urls_to_openai(
    #         ctx: Context,
    #         user: UserORM,
    #         db: AsyncIOMotorDatabase,
    #         urls: List[str],
    #         openai_client: AsyncAzureOpenAI | AsyncOpenAI,
    #         cache: Dict[str, FileObject] = dict,
    # ) -> Dict[str, FileObject]:
    #     # file_url_to_file_object_map: Dict[str, FileObject] = {}
    #     # openai_files = get_openai().openai_files
    #     openai_files = OpenaiFiles(openai_client)
    #     for url in urls:
    #         if url in cache:
    #             continue
    #         try:
    #             file_bytes: bytes | None = None
    #             file_name: str = await UserFiles.extract_file_name(ctx=ctx, file_url=url)
    #             file_ext: str = await UserFiles.extract_file_ext(ctx=ctx, file_url=url)
    #             if url.__contains__(".s3.") or url.startswith("s3://"):
    #                 s3_key = ""
    #                 s3: AwsS3 | None = None
    #                 if url.__contains__(SettingsProvider.sget().AWS_S3_BUCKET_NAME):
    #                     s3 = get_s3()
    #                 elif url.__contains__(SettingsProvider.sget().AWS_S3_PUBLIC_BUCKET_NAME):
    #                     s3 = get_public_s3()
    #                 else:
    #                     app_creds_doc = await UserAppCreds.get_app_creds_by_app_name(
    #                         ctx=Context(), db=db, user=user, app_name=AppTypes.aws
    #                     )
    #                     user_bucket_name = app_creds_doc.creds["bucket_name"]
    #                     assert user_bucket_name in url
    #                     s3 = get_aws_s3(
    #                         region_name=app_creds_doc.creds["region_name"],
    #                         aws_access_key_id=app_creds_doc.creds["aws_access_key_id"],
    #                         aws_secret_access_key=app_creds_doc.creds["aws_secret_access_key"],
    #                         bucket_name=app_creds_doc.creds["bucket_name"],
    #                         object_prefix=app_creds_doc.creds["object_prefix"],
    #                     )
    #                 assert s3 is not None
    #                 if url.__contains__(".s3."):
    #                     s3_key = await s3.get_key_from_https_url(url=url)
    #                 if url.startswith("s3://"):
    #                     s3_key = await s3.get_key_from_s3_uri(url=url)
    #                 assert s3_key
    #                 file_bytes = await s3.get_bytes(key=s3_key)
    #             elif url.startswith("http://") or url.startswith("https://"):
    #                 async with httpx.AsyncClient(follow_redirects=True) as http_client:
    #                     response = await http_client.get(url)
    #                     if response.status_code != 200:
    #                         logger.bind(ctx=ctx, url=url).error(
    #                             f"openai_responses.upload_urls_to_openai: Error in getting file from url: {url}"
    #                         )
    #                         assert response.status_code == 200
    #                     file_bytes = response.content
    #             assert file_bytes
    #
    #             if file_ext in OPENAI_FILE_FORMATS:
    #                 file_tuple = (file_name, file_bytes)
    #                 file_create = FileCreate(
    #                     file=file_tuple,
    #                     purpose="assistants",
    #                 )
    #
    #                 file_object: FileObject = await openai_files.create(
    #                     ctx=ctx, file_create=file_create
    #                 )
    #                 cache[url] = file_object
    #             else:
    #                 file_converter_config = CSV_XLSheetToTxtConfig(url=url)
    #                 text_obj = await FileConverter.csv_xls_doc_ppt_to_txt(
    #                     config=file_converter_config, ctx=ctx, user=user
    #                 )
    #                 assert isinstance(text_obj, TextObj)
    #                 file_bytes = text_obj.text.encode("utf-8")
    #
    #
    #         except Exception as e:
    #             logger.bind(ctx=ctx, url=url).exception(f"Error in upload_urls_to_openai: {str(e)}")
    #     logger.bind(ctx=ctx, file_url_to_file_object_map=cache).info(
    #         "Uploaded urls to openai"
    #     )
    #     return cache
