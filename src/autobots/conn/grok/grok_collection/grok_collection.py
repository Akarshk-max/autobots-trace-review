import asyncio
import datetime as dt
import hashlib
import inspect
import os
import tempfile
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse

import grpc
import httpx
import xai_sdk
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_types import AppTypes
from src.autobots.conn.aws.aws_s3 import get_aws_s3, get_public_s3, get_s3
from src.autobots.conn.grok.grok_collection.collection_models import (
    BatchGetDocumentsResponse,
    CharsChunkConfiguration,
    ChunkConfiguration,
    CollectionMetadata,
    DocumentFileMetadata,
    DocumentMetadata,
    FieldDefinition,
    IndexConfiguration,
    ListCollectionsResponse,
    ListDocumentsResponse,
    SearchMatch,
    SearchResponse,
    TokensChunkConfiguration,
    XAIAddExistingDocumentError,
    XAIBatchGetDocumentsError,
    XAICreateCollectionError,
    XAIDeleteCollectionError,
    XAIDocumentRemovalError,
    XAIDownloadError,
    XAIGetCollectionError,
    XAIGetDocumentError,
    XAIListCollectionsError,
    XAIListDocumentsError,
    XAIReindexError,
    XAISearchError,
    XAIUpdateCollectionError,
    XAIUpdateDocumentError,
    XAIUploadDocumentError,
)
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM

ProgressCallback = Callable[[int, int], None]


class XAICollectionsManager:
    """
    Wrapper Class for xAI Collections API:
    - Returns pydantic models
    - Strong, scoped exceptions
    - ctx-bound logging
    - Defensive SDK object adaptation
    - Includes upload_urls_to_collection (https://, s3://)
    """

    DEDUPE_FIELD_KEY = "content_sha256"

    def __init__(self) -> None:
        pass

    @staticmethod
    def _sha256_hex(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @classmethod
    def _ensure_dedupe_field_definitions(
        cls, field_definitions: Optional[list[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        fds = list(field_definitions or [])
        for fd in fds:
            if isinstance(fd, dict) and fd.get("key") == cls.DEDUPE_FIELD_KEY:
                fd["unique"] = True
                return fds

        fds.append({"key": cls.DEDUPE_FIELD_KEY, "unique": True, "required": False})
        return fds

    @staticmethod
    def _is_duplicate_error(exc: BaseException) -> bool:
        if isinstance(exc, (grpc.RpcError, grpc.aio.AioRpcError)):
            try:
                return exc.code() == grpc.StatusCode.ALREADY_EXISTS
            except Exception:
                pass

        # Fallback string matching
        msg = str(exc).lower()
        return (
            ("already exists" in msg)
            or ("already_exists" in msg)
            or ("duplicate" in msg)
            or ("unique" in msg and "exist" in msg)
        )

    async def _find_existing_by_hash(
        self,
        client: xai_sdk.AsyncClient,
        collection_id: str,
        content_hash: str,
        *,
        page_size: int = 100,
    ) -> Optional[DocumentMetadata]:
        token: Optional[str] = None
        while True:
            resp = await client.collections.list_documents(
                collection_id,
                limit=page_size,
                order="desc",
                sort_by="age",
                pagination_token=token,
            )

            for d in getattr(resp, "documents", []) or []:
                try:
                    f = getattr(d, "fields", None) or {}
                    fields_dict = dict(f)
                    if fields_dict.get(self.DEDUPE_FIELD_KEY) == content_hash:
                        return self._to_document_metadata(d)
                except Exception:
                    continue

            token = getattr(resp, "pagination_token", None)
            if not token:
                return None

    @staticmethod
    def _filter_supported_kwargs(func: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
        try:
            sig = inspect.signature(func)
        except (TypeError, ValueError):
            return kwargs

        params = sig.parameters.values()
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
            return kwargs

        allowed = set(sig.parameters.keys())
        return {k: v for k, v in kwargs.items() if k in allowed}

    async def _acall(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        cleaned = {k: v for k, v in kwargs.items() if v is not None}
        cleaned = self._filter_supported_kwargs(func, cleaned)
        return await func(*args, **cleaned)

    # Creating a new collection
    async def create(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        *,
        name: str,
        model_name: Optional[str] = None,
        chunk_configuration: Optional[dict[str, Any]] = None,
        field_definitions: Optional[list[dict[str, Any]]] = None,
    ) -> CollectionMetadata:
        try:
            field_definitions = self._ensure_dedupe_field_definitions(field_definitions)
            collection = await self._acall(
                client.collections.create,
                name=name,
                model_name=model_name,
                chunk_configuration=chunk_configuration,
                field_definitions=field_definitions,
            )

            logger.bind(ctx=ctx, user=user).info(
                f"[GROK-COLLECTION] Created collection name={collection.collection_name} id={collection.collection_id}"
            )
            return self._to_collection_metadata(collection)
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-COLLECTION] Failed to create collection error={exc}"
            )
            raise XAICreateCollectionError("Failed to create collection") from exc

    async def list_collections(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        *,
        limit: int = 10,
        order: str = "asc",
        sort_by: str = "name",
        pagination_token: Optional[str] = None,
    ) -> ListCollectionsResponse:
        try:
            response = await client.collections.list(
                limit=limit,
                order=order,
                sort_by=sort_by,
                pagination_token=pagination_token,
            )
            cols = [self._to_collection_metadata(c) for c in getattr(response, "collections", [])]
            return ListCollectionsResponse(
                collections=cols,
                pagination_token=getattr(response, "pagination_token", None),
            )
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-COLLECTION] Failed to list collections error={exc}"
            )
            raise XAIListCollectionsError("Failed to list collections") from exc

    async def get_collection(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        collection_id: str,
    ) -> CollectionMetadata:
        try:
            collection = await client.collections.get(collection_id)
            return self._to_collection_metadata(collection)
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-COLLECTION] Failed to get collection={collection_id} error={exc}"
            )
            raise XAIGetCollectionError(f"Failed to get collection {collection_id}") from exc

    async def update_collection(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        collection_id: str,
        *,
        name: Optional[str] = None,
        chunk_configuration: Optional[dict[str, Any]] = None,
        field_definitions: Optional[list[dict[str, Any]]] = None,
    ) -> CollectionMetadata:
        try:
            if field_definitions is not None:
                field_definitions = self._ensure_dedupe_field_definitions(field_definitions)

            updated = await self._acall(
                client.collections.update,
                collection_id,
                name=name,
                chunk_configuration=chunk_configuration,
                field_definitions=field_definitions,
            )

            logger.bind(ctx=ctx, user=user).info(
                f"[GROK-COLLECTION] Updated collection id={collection_id}"
            )
            return self._to_collection_metadata(updated)
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-COLLECTION] Failed to update collection={collection_id} error={exc}"
            )
            raise XAIUpdateCollectionError(f"Failed to update collection {collection_id}") from exc

    async def delete_collection(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        collection_id: str,
    ) -> None:
        try:
            await client.collections.delete(collection_id)
            logger.bind(ctx=ctx, user=user).info(
                f"[GROK-COLLECTION] Deleted collection id={collection_id}"
            )
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-COLLECTION] Failed to delete collection={collection_id} error={exc}"
            )
            raise XAIDeleteCollectionError(f"Failed to delete collection {collection_id}") from exc

    # Documents uploading to the specific collection
    async def upload_document(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        collection_id: str,
        *,
        name: str,
        data: bytes,
        wait_for_indexing: bool = False,
        poll_interval: Optional[dt.timedelta] = None,
        timeout: Optional[dt.timedelta] = None,
        content_type: Optional[str] = None,
        fields: Optional[dict[str, Any]] = None,
    ) -> DocumentMetadata:
        try:
            content_hash = self._sha256_hex(data)
            merged_fields = dict(fields or {})
            merged_fields.setdefault(self.DEDUPE_FIELD_KEY, content_hash)

            # if already present, skip upload silently
            existing = await self._find_existing_by_hash(
                client=client,
                collection_id=collection_id,
                content_hash=content_hash,
            )
            if existing:
                # silently ignore keep log at debug level
                logger.bind(ctx=ctx, user=user).debug(
                    f"[GROK-COLLECTION] Duplicate detected, skipping upload "
                    f"collection_id={collection_id} hash={content_hash} existing_file_id={existing.file_metadata.file_id}"
                )
                return existing

            document = await self._acall(
                client.collections.upload_document,
                collection_id,
                name=name,
                data=data,
                wait_for_indexing=wait_for_indexing,
                poll_interval=poll_interval,
                timeout=timeout,
                content_type=content_type,
                fields=merged_fields,
            )

            logger.bind(ctx=ctx, user=user).info(
                f"[GROK-COLLECTION] Uploaded document name={getattr(document.file_metadata, 'name', name)} "
                f"file_id={getattr(document.file_metadata, 'file_id', None)} collection_id={collection_id}"
            )
            return self._to_document_metadata(document)

        except Exception as exc:
            if self._is_duplicate_error(exc):
                existing = await self._find_existing_by_hash(
                    client=client,
                    collection_id=collection_id,
                    content_hash=self._sha256_hex(data),
                )
                if existing:
                    logger.bind(ctx=ctx, user=user).debug(
                        f"[GROK-COLLECTION] Duplicate upload raced, ignoring. "
                        f"collection_id={collection_id} existing_file_id={existing.file_metadata.file_id}"
                    )
                    return existing

                # still no error - very rare because i checked whole documentation for this did not get anything better than this.
                logger.bind(ctx=ctx, user=user).debug(
                    "[GROK-COLLECTION] Duplicate suspected but existing doc not found; ignoring."
                )
                return DocumentMetadata(
                    file_metadata=DocumentFileMetadata(
                        file_id="",
                        name=name,
                        size_bytes=len(data),
                        content_type=content_type,
                    ),
                    status="DUPLICATE_IGNORED",
                    fields=dict(fields or {}),
                )

            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-COLLECTION] Failed to upload document to collection={collection_id} error={exc}"
            )
            raise XAIUploadDocumentError("Failed to upload document") from exc

    async def add_existing_document(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        collection_id: str,
        file_id: str,
        *,
        fields: Optional[dict[str, Any]] = None,
    ) -> None:
        try:
            await client.collections.add_existing_document(collection_id, file_id, fields=fields)
            logger.bind(ctx=ctx, user=user).info(
                f"[GROK-COLLECTION] Added existing document file_id={file_id} to collection_id={collection_id}"
            )
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-COLLECTION] Failed to add existing document file_id={file_id} collection_id={collection_id} error={exc}"
            )
            raise XAIAddExistingDocumentError(f"Failed to add existing document {file_id}") from exc

    async def list_documents(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        collection_id: str,
        *,
        limit: int = 10,
        order: str = "desc",
        sort_by: str = "age",
        pagination_token: Optional[str] = None,
    ) -> ListDocumentsResponse:
        try:
            response = await client.collections.list_documents(
                collection_id,
                limit=limit,
                order=order,
                sort_by=sort_by,
                pagination_token=pagination_token,
            )
            docs = [self._to_document_metadata(d) for d in getattr(response, "documents", [])]
            return ListDocumentsResponse(
                documents=docs,
                pagination_token=getattr(response, "pagination_token", None),
            )
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-COLLECTION] Failed to list documents collection_id={collection_id} error={exc}"
            )
            raise XAIListDocumentsError(
                f"Failed to list documents for collection {collection_id}"
            ) from exc

    async def get_document(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        collection_id: str,
        file_id: str,
    ) -> DocumentMetadata:
        try:
            doc = await client.collections.get_document(file_id, collection_id)
            return self._to_document_metadata(doc)
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-COLLECTION] Failed to get document file_id={file_id} collection_id={collection_id} error={exc}"
            )
            raise XAIGetDocumentError(f"Failed to get document {file_id}") from exc

    async def batch_get_documents(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        collection_id: str,
        file_ids: list[str],
    ) -> BatchGetDocumentsResponse:
        try:
            response = await client.collections.batch_get_documents(collection_id, file_ids)
            docs = [self._to_document_metadata(d) for d in getattr(response, "documents", [])]
            return BatchGetDocumentsResponse(documents=docs)
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-COLLECTION] Failed batch_get_documents collection_id={collection_id} error={exc}"
            )
            raise XAIBatchGetDocumentsError("Failed to batch get documents") from exc

    async def update_document(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        collection_id: str,
        file_id: str,
        *,
        name: Optional[str] = None,
        data: Optional[bytes] = None,
        content_type: Optional[str] = None,
        fields: Optional[dict[str, Any]] = None,
    ) -> DocumentMetadata:
        try:
            updated = await self._acall(
                client.collections.update_document,
                collection_id,
                file_id,
                name=name,
                data=data,
                content_type=content_type,
                fields=fields,
            )

            logger.bind(ctx=ctx, user=user).info(
                f"[GROK-COLLECTION] Updated document file_id={file_id} collection_id={collection_id}"
            )
            return self._to_document_metadata(updated)
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-COLLECTION] Failed to update document file_id={file_id} error={exc}"
            )
            raise XAIUpdateDocumentError(f"Failed to update document {file_id}") from exc

    async def reindex_document(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        collection_id: str,
        file_id: str,
    ) -> None:
        try:
            await client.collections.reindex_document(collection_id, file_id)
            logger.bind(ctx=ctx, user=user).info(
                f"[GROK-COLLECTION] Reindexed document file_id={file_id} collection_id={collection_id}"
            )
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-COLLECTION] Failed to reindex document file_id={file_id} error={exc}"
            )
            raise XAIReindexError(f"Failed to reindex document {file_id}") from exc

    async def remove_document(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        collection_id: str,
        file_id: str,
    ) -> None:
        try:
            await client.collections.remove_document(collection_id, file_id)
            logger.bind(ctx=ctx, user=user).info(
                f"[GROK-COLLECTION] Removed document file_id={file_id} from collection_id={collection_id}"
            )
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-COLLECTION] Failed to remove document file_id={file_id} error={exc}"
            )
            raise XAIDocumentRemovalError(f"Failed to remove document {file_id}") from exc

    # Search query in collection
    async def search(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        *,
        query: str,
        collection_ids: list[str],
        limit: int = 5,
        instructions: Optional[str] = None,
        retrieval_mode: str = "hybrid",
    ) -> SearchResponse:
        try:
            results = await client.collections.search(
                query=query,
                collection_ids=collection_ids,
                limit=limit,
                instructions=instructions,
                retrieval_mode=retrieval_mode,
            )
            matches = [self._to_search_match(m) for m in getattr(results, "matches", [])]
            return SearchResponse(matches=matches)
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-COLLECTION] Search failed error={exc}"
            )
            raise XAISearchError("Search failed") from exc

    # Upload URLs to the specific given collection id
    async def upload_urls_to_collection(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        collection_id: str,
        urls: list[str],
        *,
        batch_size: int = 3,
        wait_for_indexing: bool = False,
        poll_interval: Optional[dt.timedelta] = None,
        timeout: Optional[dt.timedelta] = None,
        on_progress: ProgressCallback | None = None,
        on_file_complete: Callable[[int, str, object], None] | None = None,
        timeout_sec: float = 120.0,
        max_bytes: Optional[int] = None,
        fields: Optional[dict[str, Any]] = None,
    ) -> dict[str, DocumentMetadata]:
        """
        Download from s3:// or https:// URLs to disk (streaming) and upload to xAI collection.
        - Rejects http:// URLs.
        - For https://, blocks redirect to http://.
        - Reuses the same S3 credential resolution logic you used in XAIFileManager.

        Returns: {url: DocumentMetadata} for successful uploads. Failed ones are omitted.
        """
        sem = asyncio.Semaphore(batch_size)
        results: dict[str, object] = {}

        async def _one(idx: int, url: str) -> None:
            async with sem:
                try:
                    (
                        tmp_path,
                        inferred_filename,
                        inferred_content_type,
                    ) = await self._download_url_to_tempfile(
                        ctx=ctx,
                        user=user,
                        db=db,
                        url=url,
                        on_progress=on_progress,
                        timeout_sec=timeout_sec,
                    )

                    try:
                        # Load bytes for xai_sdk upload_document(data=bytes)
                        size = os.path.getsize(tmp_path)
                        if max_bytes is not None and size > max_bytes:
                            raise XAIDownloadError(
                                f"Downloaded file too large: {size} bytes exceeds max_bytes={max_bytes}"
                            )

                        with open(tmp_path, "rb") as f:
                            data = f.read()

                        doc = await self.upload_document(
                            ctx=ctx,
                            user=user,
                            db=db,
                            client=client,
                            collection_id=collection_id,
                            name=inferred_filename or os.path.basename(tmp_path),
                            data=data,
                            wait_for_indexing=wait_for_indexing,
                            poll_interval=poll_interval,
                            timeout=timeout,
                            content_type=inferred_content_type,
                            fields=fields,
                        )

                        results[url] = doc
                        if on_file_complete:
                            on_file_complete(idx, url, doc)

                    finally:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass

                except Exception as exc:
                    results[url] = exc
                    logger.bind(ctx=ctx, user=user).exception(
                        f"[GROK-COLLECTION] upload_urls_to_collection failed url={url} error={exc}"
                    )
                    if on_file_complete:
                        on_file_complete(idx, url, exc)

        tasks = [asyncio.create_task(_one(i, u)) for i, u in enumerate(urls)]
        await asyncio.gather(*tasks)

        output: dict[str, DocumentMetadata] = {}
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
    ) -> tuple[str, Optional[str], Optional[str]]:
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

    async def _download_https_to_tempfile(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        url: str,
        *,
        on_progress: ProgressCallback | None,
        timeout_sec: float,
    ) -> tuple[str, Optional[str], Optional[str]]:
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

                    cl = r.headers.get("content-length")
                    if cl and cl.isdigit():
                        total = int(cl)

                    content_type = r.headers.get("content-type")
                    if content_type:
                        # Strip charset if present: "text/plain; charset=utf-8"
                        content_type = content_type.split(";", 1)[0].strip()

                    with open(tmp_path, "wb") as f:
                        async for chunk in r.aiter_bytes(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            f.write(chunk)
                            bytes_read += len(chunk)
                            if on_progress:
                                on_progress(bytes_read, total)

            if on_progress and total == 0:
                on_progress(bytes_read, bytes_read)

            return tmp_path, filename, content_type if "content_type" in locals() else None

        except Exception as exc:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-COLLECTION] Failed to download https url={url} error={exc}"
            )
            raise XAIDownloadError(f"Failed to download from {url}") from exc

    async def _download_s3_to_tempfile(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        url: str,
        *,
        on_progress: ProgressCallback | None,
    ) -> tuple[str, Optional[str], Optional[str]]:
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

            def _download_blocking() -> None:
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

            # content_type unknown for s3 unless you add a head_object method; keep None.
            return tmp_path, filename, None

        except Exception as exc:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-COLLECTION] Failed to download s3 url={url} error={exc}"
            )
            raise XAIDownloadError(f"Failed to download from {url}") from exc

    # Internal adapters
    @staticmethod
    def _to_datetime(ts_obj: Any) -> Optional[dt.datetime]:
        if ts_obj is None:
            return None
        try:
            if hasattr(ts_obj, "ToDatetime") and callable(ts_obj.ToDatetime):
                return ts_obj.ToDatetime()
            if isinstance(ts_obj, dt.datetime):
                return ts_obj
        except Exception:
            return None
        return None

    def _to_collection_metadata(self, collection: Any) -> CollectionMetadata:
        idx_conf = None
        try:
            ic = getattr(collection, "index_configuration", None)
            if ic is not None:
                idx_conf = IndexConfiguration(model_name=getattr(ic, "model_name", None))
        except Exception:
            idx_conf = None

        chunk_conf = self._to_chunk_configuration(getattr(collection, "chunk_configuration", None))

        field_defs: Optional[list[FieldDefinition]] = None
        try:
            raw = getattr(collection, "field_definitions", None)
            if raw:
                field_defs = [
                    FieldDefinition(
                        key=getattr(f, "key", ""),
                        required=bool(getattr(f, "required", False)),
                        unique=bool(getattr(f, "unique", False)),
                    )
                    for f in raw
                ]
        except Exception:
            field_defs = None

        return CollectionMetadata(
            collection_id=getattr(collection, "collection_id", ""),
            collection_name=getattr(collection, "collection_name", ""),
            documents_count=int(getattr(collection, "documents_count", 0) or 0),
            created_at=self._to_datetime(getattr(collection, "created_at", None)),
            index_configuration=idx_conf,
            chunk_configuration=chunk_conf,
            field_definitions=field_defs,
        )

    def _to_chunk_configuration(self, cfg: Any) -> Optional[ChunkConfiguration]:
        if cfg is None:
            return None

        tokens_cfg = None
        chars_cfg = None

        try:
            tc = getattr(cfg, "tokens_configuration", None)
            if tc is not None:
                tokens_cfg = TokensChunkConfiguration(
                    max_chunk_size_tokens=int(getattr(tc, "max_chunk_size_tokens", 0) or 0),
                    chunk_overlap_tokens=int(getattr(tc, "chunk_overlap_tokens", 0) or 0),
                    encoding_name=getattr(tc, "encoding_name", None),
                )
        except Exception:
            tokens_cfg = None

        try:
            cc = getattr(cfg, "chars_configuration", None)
            if cc is not None:
                chars_cfg = CharsChunkConfiguration(
                    max_chunk_size_chars=int(getattr(cc, "max_chunk_size_chars", 0) or 0),
                    chunk_overlap_chars=int(getattr(cc, "chunk_overlap_chars", 0) or 0),
                )
        except Exception:
            chars_cfg = None

        return ChunkConfiguration(
            tokens_configuration=tokens_cfg,
            chars_configuration=chars_cfg,
            strip_whitespace=getattr(cfg, "strip_whitespace", None),
        )

    @staticmethod
    def _pb_enum_name(msg: Any, field_name: str, value: int) -> Optional[str]:
        try:
            desc = getattr(msg, "DESCRIPTOR", None)
            if desc is None:
                return None

            field = desc.fields_by_name.get(field_name)
            if field is None or field.enum_type is None:
                return None

            enum_val = field.enum_type.values_by_number.get(int(value))
            return enum_val.name if enum_val is not None else None
        except Exception:
            return None

    def _to_document_metadata(self, doc: Any) -> DocumentMetadata:
        fm = getattr(doc, "file_metadata", None)

        file_meta = DocumentFileMetadata(
            file_id=getattr(fm, "file_id", "") if fm is not None else "",
            name=getattr(fm, "name", "") if fm is not None else "",
            size_bytes=int(getattr(fm, "size_bytes", 0) or 0) if fm is not None else 0,
            content_type=getattr(fm, "content_type", None) if fm is not None else None,
        )

        fields_dict: dict[str, Any] = {}
        try:
            f = getattr(doc, "fields", None)
            if f:
                fields_dict = dict(f)
        except Exception:
            fields_dict = {}

        raw_status = getattr(doc, "status", None)
        status_str: Optional[str]
        if raw_status is None:
            status_str = None
        elif isinstance(raw_status, str):
            status_str = raw_status
        elif isinstance(raw_status, int):
            status_str = self._pb_enum_name(doc, "status", raw_status) or str(raw_status)
        else:
            status_str = getattr(raw_status, "name", None) or str(raw_status)

        return DocumentMetadata(
            file_metadata=file_meta,
            status=status_str,
            fields=fields_dict,
        )

    def _to_search_match(self, match: Any) -> SearchMatch:
        return SearchMatch(
            file_id=getattr(match, "file_id", ""),
            score=float(getattr(match, "score", 0.0) or 0.0),
            chunk_content=getattr(match, "chunk_content", "") or "",
        )
