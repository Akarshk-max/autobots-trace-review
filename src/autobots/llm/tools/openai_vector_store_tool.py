import asyncio
import json
import os
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, TypedDict, Union
from urllib.parse import unquote, urlparse

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    ConflictError,
    InternalServerError,
    RateLimitError,
)
from openai.types import FileObject
from pydantic import BaseModel, ConfigDict, Field, model_validator
from src.autobots.data_model.app_state import AppState

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.integrations.app_conn_rule.user_app_conn_rule import UserAppConnRule
from src.autobots.conn.openai.openai_client import OpenAI, get_conn_openai
from src.autobots.conn.openai.openai_files.openai_files import OpenaiFiles
from src.autobots.conn.openai.openai_files.openai_files_model import FileCreate
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.files.file_downloader import FileDownloader
from src.autobots.files.user_files import UserFiles
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM

RETRYABLE_OPENAI_EXCEPTIONS = (
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    InternalServerError,
    ConflictError,
)
MAX_PARALLEL_UPLOADS = 5

class UploadSuccess(TypedDict):
    source: str
    status: str
    openai_file_id: str

class UploadFailure(TypedDict):
    source: str
    status: str
    reason: str

UploadOutcome = UploadSuccess | UploadFailure

AttributeValue = Union[str, int, float, bool]
class OpenAIVectorStoreToolOperation(str, Enum):
    CREATE_VECTOR_STORE = "create_vector_store"
    ADD_FILES = "add_files"
    REINDEX_FILES = "reindex_files"
    QUERY_VECTOR_STORE = "query_vector_store"
    GET_VECTOR_STORE = "get_vector_store"
    LIST_VECTOR_STORE_FILES = "list_vector_store_files"
    GET_FILE_BATCH = "get_file_batch"
    DELETE_VECTOR_STORE_FILES = "delete_vector_store_files"
    DELETE_VECTOR_STORE = "delete_vector_store"


class OpenAIVectorStoreChunkingAuto(BaseModel):
    type: Literal["auto"] = "auto"

    def to_openai_dict(self) -> Dict[str, Any]:
        return {"type": "auto"}


class OpenAIVectorStoreChunkingStatic(BaseModel):
    type: Literal["static"] = "static"
    max_chunk_size_tokens: int = Field(..., ge=100, le=4096)
    chunk_overlap_tokens: int = Field(..., ge=0)

    @model_validator(mode="after")
    def validate_overlap(self) -> "OpenAIVectorStoreChunkingStatic":
        if self.chunk_overlap_tokens > (self.max_chunk_size_tokens // 2):
            raise ValueError("chunk_overlap_tokens must not exceed max_chunk_size_tokens / 2")
        return self

    def to_openai_dict(self) -> Dict[str, Any]:
        return {
            "type": "static",
            "static": {
                "max_chunk_size_tokens": self.max_chunk_size_tokens,
                "chunk_overlap_tokens": self.chunk_overlap_tokens,
            },
        }


OpenAIVectorStoreChunkingStrategy = Union[
    OpenAIVectorStoreChunkingAuto,
    OpenAIVectorStoreChunkingStatic,
]


class OpenAIVectorStoreFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    openai_file_id: str | None = None
    attributes: Dict[str, AttributeValue] = Field(default_factory=dict)
    chunking_strategy: OpenAIVectorStoreChunkingStrategy | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "OpenAIVectorStoreFileInput":
        if bool(self.url) == bool(self.openai_file_id):
            raise ValueError("Exactly one of `url` or `openai_file_id` must be provided")
        return self


class OpenAIVectorStoreToolConfig(BaseModel):
    """
    Standalone OpenAI vector-store retrieval tool config.

    Notes:
    - create_vector_store: creates and returns a vector_store_id
    - add_files: uploads/attaches files into an existing vector store
    - reindex_files: explicitly deletes selected vector_store file attachments, then re-adds files
    - query_vector_store: direct search API, usable by any LLM
    """
    model_config = ConfigDict(extra="forbid")

    operation: OpenAIVectorStoreToolOperation

    # Common
    vector_store_id: str | None = None

    # Create vector store
    name: str | None = None
    expires_after_days: int | None = Field(default=None, ge=1, le=365)
    metadata: Dict[str, AttributeValue] = Field(default_factory=dict)

    # Add / reindex
    files: List[OpenAIVectorStoreFileInput] = Field(default_factory=list)
    batch_attributes: Dict[str, AttributeValue] = Field(default_factory=dict)
    batch_chunking_strategy: OpenAIVectorStoreChunkingStrategy | None = None
    wait_for_completion: bool = True
    poll_interval_sec: int = Field(default=2, ge=1, le=30)
    poll_timeout_sec: int = Field(default=900, ge=10, le=7200)

    # Reindex / delete specific vector_store.file attachments
    delete_file_ids: List[str] = Field(default_factory=list) # vector-store attachment IDs
    delete_underlying_openai_files: bool = False

    # Search
    query: str | None = None
    max_num_results: int = Field(default=10, ge=1, le=50)
    rewrite_query: bool = True
    filters: Dict[str, Any] | None = None
    ranking_options: Dict[str, Any] | None = None

    # Batch / list helpers
    batch_id: str | None = None
    limit: int = Field(default=100, ge=1, le=100)

    @model_validator(mode="after")
    def validate_by_operation(self) -> "OpenAIVectorStoreToolConfig":
        op = self.operation

        if op == OpenAIVectorStoreToolOperation.CREATE_VECTOR_STORE:
            if not self.name:
                raise ValueError("`name` is required for create_vector_store")

        elif op == OpenAIVectorStoreToolOperation.ADD_FILES:
            if not self.vector_store_id:
                raise ValueError("`vector_store_id` is required for add_files")
            if not self.files:
                raise ValueError("`files` is required for add_files")

        elif op == OpenAIVectorStoreToolOperation.REINDEX_FILES:
            if not self.vector_store_id:
                raise ValueError("`vector_store_id` is required for reindex_files")
            if not self.delete_file_ids and not self.files:
                raise ValueError(
                    "reindex_files requires at least one of `delete_file_ids` or `files`"
                )

        elif op == OpenAIVectorStoreToolOperation.QUERY_VECTOR_STORE:
            if not self.vector_store_id:
                raise ValueError("`vector_store_id` is required for query_vector_store")
            if not self.query:
                raise ValueError("`query` is required for query_vector_store")

        elif op in {
            OpenAIVectorStoreToolOperation.GET_VECTOR_STORE,
            OpenAIVectorStoreToolOperation.LIST_VECTOR_STORE_FILES,
            OpenAIVectorStoreToolOperation.DELETE_VECTOR_STORE,
            OpenAIVectorStoreToolOperation.DELETE_VECTOR_STORE_FILES,
        }:
            if not self.vector_store_id:
                raise ValueError(f"`vector_store_id` is required for {op.value}")

        elif op == OpenAIVectorStoreToolOperation.GET_FILE_BATCH:
            if not self.vector_store_id:
                raise ValueError("`vector_store_id` is required for get_file_batch")
            if not self.batch_id:
                raise ValueError("`batch_id` is required for get_file_batch")

        if op == OpenAIVectorStoreToolOperation.DELETE_VECTOR_STORE_FILES and not self.delete_file_ids:
            raise ValueError("`delete_file_ids` is required for delete_vector_store_files")

        # In this app we only remove files from the current vector store.
        # Never delete the underlying OpenAI file, because that would affect
        # every vector store that references it.
        self.delete_underlying_openai_files = False

        return self    

OPENAI_VECTOR_STORE_SUPPORTED_EXTENSIONS = {
    ".c",
    ".cpp",
    ".cs",
    ".css",
    ".doc",
    ".docx",
    ".go",
    ".html",
    ".java",
    ".js",
    ".json",
    ".md",
    ".pdf",
    ".php",
    ".pptx",
    ".py",
    ".rb",
    ".sh",
    ".tex",
    ".ts",
    ".txt",
}

OPENAI_VECTOR_STORE_AUTO_INDEX_DENY_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".xls",
    ".xlsx",
}

TERMINAL_BATCH_STATUSES = {"completed", "failed", "cancelled"}


class OpenAIVectorStoreToolService:
    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
    ):
        self.client = client
        self.ctx = ctx
        self.user = user
        self.db = db
        self.notify = notify

    async def execute(self, config: OpenAIVectorStoreToolConfig) -> Dict[str, Any]:
        op = config.operation

        if op == OpenAIVectorStoreToolOperation.CREATE_VECTOR_STORE:
            return await self.create_vector_store(config=config)

        if op == OpenAIVectorStoreToolOperation.ADD_FILES:
            return await self.add_files(config=config)

        if op == OpenAIVectorStoreToolOperation.REINDEX_FILES:
            return await self.reindex_files(config=config)

        if op == OpenAIVectorStoreToolOperation.QUERY_VECTOR_STORE:
            return await self.query_vector_store(config=config)

        if op == OpenAIVectorStoreToolOperation.GET_VECTOR_STORE:
            return await self.get_vector_store(config=config)

        if op == OpenAIVectorStoreToolOperation.LIST_VECTOR_STORE_FILES:
            return await self.list_vector_store_files(config=config)

        if op == OpenAIVectorStoreToolOperation.GET_FILE_BATCH:
            return await self.get_file_batch(config=config)

        if op == OpenAIVectorStoreToolOperation.DELETE_VECTOR_STORE_FILES:
            return await self.delete_vector_store_files(config=config)

        if op == OpenAIVectorStoreToolOperation.DELETE_VECTOR_STORE:
            return await self.delete_vector_store(config=config)

        raise HTTPException(status_code=400, detail=f"Unsupported operation: {op}")
    

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(RETRYABLE_OPENAI_EXCEPTIONS),
        reraise=True,
    )
    async def _create_file_batch_with_retry(
        self,
        *,
        vector_store_id: str,
        files: List[Dict[str, Any]],
    ):
        return await self.client.vector_stores.file_batches.create(
            vector_store_id=vector_store_id,
            files=files,
        )

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(RETRYABLE_OPENAI_EXCEPTIONS),
        reraise=True,
    )
    async def _retrieve_file_batch_with_retry(
        self,
        *,
        vector_store_id: str,
        batch_id: str,
    ):
        return await self.client.vector_stores.file_batches.retrieve(
            vector_store_id=vector_store_id,
            batch_id=batch_id,
        )

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(RETRYABLE_OPENAI_EXCEPTIONS),
        reraise=True,
    )
    async def _list_vector_store_files_with_retry(
        self,
        *,
        vector_store_id: str,
        limit: int,
    ):
        return await self.client.vector_stores.files.list(
            vector_store_id=vector_store_id,
            limit=limit,
        )

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(RETRYABLE_OPENAI_EXCEPTIONS),
        reraise=True,
    )
    async def _create_vector_store_with_retry(self, payload: Dict[str, Any]):
        return await self.client.vector_stores.create(**payload)

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(RETRYABLE_OPENAI_EXCEPTIONS),
        reraise=True,
    )
    async def _search_vector_store_with_retry(self, payload: Dict[str, Any]):
        return await self.client.vector_stores.search(**payload)

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(RETRYABLE_OPENAI_EXCEPTIONS),
        reraise=True,
    )
    async def _retrieve_vector_store_with_retry(self, *, vector_store_id: str):
        return await self.client.vector_stores.retrieve(vector_store_id=vector_store_id)

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(RETRYABLE_OPENAI_EXCEPTIONS),
        reraise=True,
    )
    async def _delete_vector_store_with_retry(self, *, vector_store_id: str):
        return await self.client.vector_stores.delete(vector_store_id=vector_store_id)

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(RETRYABLE_OPENAI_EXCEPTIONS),
        reraise=True,
    )
    async def _delete_vector_store_file_with_retry(self, *, vector_store_id: str, file_id: str):
        return await self.client.vector_stores.files.delete(
            vector_store_id=vector_store_id,
            file_id=file_id,
        )

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(RETRYABLE_OPENAI_EXCEPTIONS),
        reraise=True,
    )
    async def _delete_openai_file_with_retry(self, *, file_id: str):
        return await self.client.files.delete(file_id=file_id)

    @retry(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(RETRYABLE_OPENAI_EXCEPTIONS),
            reraise=True,
        )
    async def _upload_single_tmpfile_to_openai(
        self,
        *,
        openai_files: OpenaiFiles,
        url: str,
        tmp_path: str,
    ) -> FileObject:
        file_name = await self._extract_file_name(url=url, tmp_path=tmp_path)
        with open(tmp_path, "rb") as f:
            file_bytes = f.read()

        file_create = FileCreate(
            file=(file_name, file_bytes),
            purpose="assistants",
        )
        return await openai_files.create(ctx=self.ctx, file_create=file_create)

    async def create_vector_store(self, config: OpenAIVectorStoreToolConfig) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "name": config.name,
        }
        if config.metadata:
            payload["metadata"] = config.metadata
        if config.expires_after_days:
            payload["expires_after"] = {
                "anchor": "last_active_at",
                "days": config.expires_after_days,
            }

        self._notify(f"Creating vector store: {config.name}")
        vector_store = await self._create_vector_store_with_retry(payload)
        dumped = self._dump(vector_store)

        return {
            "ok": True,
            "operation": config.operation.value,
            "vector_store_id": dumped.get("id"),
            "vector_store_name": dumped.get("name"),
            "vector_store": dumped,
        }
    
    async def _cleanup_uploaded_openai_files(self, *, file_ids: List[str]) -> None:
        for file_id in file_ids:
            try:
                await self._delete_openai_file_with_retry(file_id=file_id)
            except Exception as e:
                logger.bind(ctx=self.ctx, file_id=file_id).warning(
                    f"Failed cleaning up OpenAI file after batch failure: {str(e)}"
                )

    async def _list_vector_store_file_attachments(
        self,
        *,
        vector_store_id: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        paginator = await self._list_vector_store_files_with_retry(
            vector_store_id=vector_store_id,
            limit=limit,
        )

        items: List[Dict[str, Any]] = []
        async for item in paginator:
            items.append(self._dump(item))
            if len(items) >= limit:
                break

        return items

    def _match_indexed_item_to_attachment(
        self,
        *,
        indexed_item: Dict[str, Any],
        attachments: List[Dict[str, Any]],
    ) -> Dict[str, Any] | None:
        indexed_source_url = str(indexed_item.get("source_url") or "").strip()
        indexed_prepared_url = str(indexed_item.get("prepared_url") or "").strip()
        indexed_openai_file_id = str(indexed_item.get("openai_file_id") or "").strip()

        # First prefer deterministic matching through attributes we already send.
        for attachment in attachments:
            attributes = attachment.get("attributes")
            if not isinstance(attributes, dict):
                continue

            attachment_source_url = str(attributes.get("source_url") or "").strip()
            attachment_prepared_url = str(attributes.get("prepared_url") or "").strip()

            if indexed_prepared_url and attachment_prepared_url == indexed_prepared_url:
                return attachment

            if indexed_source_url and attachment_source_url == indexed_source_url:
                return attachment

        # Fallback to API ids if present in the returned object.
        for attachment in attachments:
            attachment_file_id = str(attachment.get("file_id") or "").strip()
            attachment_id = str(attachment.get("id") or "").strip()

            if indexed_openai_file_id and attachment_file_id == indexed_openai_file_id:
                return attachment

            if indexed_openai_file_id and attachment_id == indexed_openai_file_id:
                return attachment

        return None

    async def _list_vector_store_files_by_attachment_id(
        self,
        *,
        vector_store_id: str,
        attachment_ids: set[str] | None = None,
        limit: int = 100,
    ) -> Dict[str, Dict[str, Any]]:
        paginator = await self._list_vector_store_files_with_retry(
            vector_store_id=vector_store_id,
            limit=limit,
        )

        out: Dict[str, Dict[str, Any]] = {}
        target_ids = {str(x).strip() for x in (attachment_ids or set()) if str(x).strip()}

        async for item in paginator:
            dumped = self._dump(item)
            attachment_id = str(dumped.get("id") or "").strip()
            if not attachment_id:
                continue
            if target_ids and attachment_id not in target_ids:
                continue

            out[attachment_id] = dumped

            if target_ids and len(out) >= len(target_ids):
                break

        return out
    
    async def add_files(self, config: OpenAIVectorStoreToolConfig) -> Dict[str, Any]:
        self._notify(f"Preparing {len(config.files)} file(s) for ingestion")

        resolved = await self._resolve_openai_file_ids(files=config.files)
        resolved_file_ids = resolved["resolved_file_ids"]
        successful_files = resolved["successful_files"]
        reused_files = resolved["reused_files"]
        failed_files = resolved["failed_files"]

        batch_files: List[Dict[str, Any]] = []
        indexed_files: List[Dict[str, Any]] = []

        for file_item in config.files:
            source_key = self._source_key(file_item)
            file_id = resolved_file_ids.get(source_key)
            if not file_id:
                continue

            effective_attributes = self._merge_attributes(
                config.batch_attributes,
                file_item.attributes,
            )
            effective_chunking = file_item.chunking_strategy or config.batch_chunking_strategy

            entry: Dict[str, Any] = {"file_id": file_id}
            if effective_attributes:
                entry["attributes"] = effective_attributes
            if effective_chunking:
                entry["chunking_strategy"] = self._chunking_to_dict(effective_chunking)

            batch_files.append(entry)
            indexed_files.append(
                {
                    "source_url": file_item.url,
                    "source_openai_file_id": file_item.openai_file_id,
                    "prepared_url": file_item.url,
                    "openai_file_id": file_id,
                    "vector_store_file_id": None,
                    "attributes": effective_attributes,
                    "status": "pending",
                }
            )

        if not batch_files:
            return {
                "ok": False,
                "operation": config.operation.value,
                "vector_store_id": config.vector_store_id,
                "batch": None,
                "indexed_files": [],
                "successful_files": [],
                "failed_files": failed_files,
                "message": "No files were successfully uploaded; batch was not created.",
            }

        try:
            self._notify(f"Creating ingestion batch in vector store {config.vector_store_id}")
            created_batch = await self._create_file_batch_with_retry(
                vector_store_id=config.vector_store_id,
                files=batch_files,
            )

            if config.wait_for_completion:
                self._notify(f"Polling batch {created_batch.id} until completion")
                created_batch = await self._poll_file_batch(
                    vector_store_id=config.vector_store_id,
                    batch_id=created_batch.id,
                    poll_interval_sec=config.poll_interval_sec,
                    poll_timeout_sec=config.poll_timeout_sec,
                )

            batch_dump = self._dump(created_batch)
            batch_status = str(batch_dump.get("status") or "").lower()
            normalized_status = "indexed" if batch_status == "completed" else (batch_status or "pending")

            attachments: List[Dict[str, Any]] = []
            if batch_status == "completed":
                attachments = await self._list_vector_store_file_attachments(
                    vector_store_id=config.vector_store_id,
                    limit=max(100, len(indexed_files) * 3),
                )

            for item in indexed_files:
                item["status"] = normalized_status

                attachment = self._match_indexed_item_to_attachment(
                    indexed_item=item,
                    attachments=attachments,
                )
                if attachment:
                    item["vector_store_file_id"] = str(
                        attachment.get("id")
                        or attachment.get("file_id")
                        or ""
                    ).strip() or None

                    item["status"] = (
                        str(attachment.get("status") or item["status"] or "").lower()
                        or item["status"]
                    )
                    if attachment.get("attributes"):
                        item["attributes"] = attachment.get("attributes")

            return {
                "ok": len(failed_files) == 0 and batch_status == "completed",
                "partial_success": len(failed_files) > 0 or batch_status not in {"", "completed"},
                "operation": config.operation.value,
                "vector_store_id": config.vector_store_id,
                "batch": batch_dump,
                "indexed_files": indexed_files,
                "successful_files": successful_files,
                "reused_files": reused_files,
                "failed_files": failed_files,
            }

        except Exception:
            await self._cleanup_uploaded_openai_files(
                file_ids=[x["openai_file_id"] for x in successful_files if "openai_file_id" in x]
            )
            raise

    async def reindex_files(self, config: OpenAIVectorStoreToolConfig) -> Dict[str, Any]:
        delete_result: Dict[str, Any] | None = None
        add_result: Dict[str, Any] | None = None

        if config.delete_file_ids:
            self._notify(f"Deleting {len(config.delete_file_ids)} vector-store file attachment(s)")
            delete_result = await self._delete_vector_store_files_internal(
                vector_store_id=config.vector_store_id,
                delete_file_ids=config.delete_file_ids,
                delete_underlying_openai_files=config.delete_underlying_openai_files,
            )

        if config.files:
            add_cfg = config.model_copy(
                update={"operation": OpenAIVectorStoreToolOperation.ADD_FILES}
            )
            add_result = await self.add_files(config=add_cfg)

        delete_errors = 0
        if delete_result:
            delete_errors += sum(1 for x in delete_result.get("deleted_attachments", []) if "error" in x)
            delete_errors += sum(1 for x in delete_result.get("deleted_openai_files", []) if "error" in x)

        add_ok = True if add_result is None else bool(add_result.get("ok", False))
        add_partial = False if add_result is None else bool(add_result.get("partial_success", False))

        ok = delete_errors == 0 and add_ok
        partial_success = add_partial or (delete_errors > 0 and (delete_result is not None or add_result is not None))

        return {
            "ok": ok,
            "partial_success": partial_success,
            "operation": config.operation.value,
            "vector_store_id": config.vector_store_id,
            "delete_result": delete_result,
            "add_result": add_result,
        }

    async def query_vector_store(self, config: OpenAIVectorStoreToolConfig) -> Dict[str, Any]:
        self._notify("Searching vector store")

        search_payload: Dict[str, Any] = {
            "vector_store_id": config.vector_store_id,
            "query": config.query,
            "max_num_results": config.max_num_results,
            "rewrite_query": config.rewrite_query,
        }
        if config.filters:
            search_payload["filters"] = config.filters
        if config.ranking_options:
            search_payload["ranking_options"] = config.ranking_options

        results = await self._search_vector_store_with_retry(search_payload)

        compact_results = []
        for item in getattr(results, "data", []):
            compact_results.append(
                {
                    "file_id": getattr(item, "file_id", None),
                    "filename": getattr(item, "filename", None),
                    "score": getattr(item, "score", None),
                    "attributes": getattr(item, "attributes", None),
                    "content": [
                        getattr(chunk, "text", None)
                        for chunk in getattr(item, "content", [])
                        if getattr(chunk, "text", None)
                    ],
                }
            )

        return {
            "ok": True,
            "operation": config.operation.value,
            "vector_store_id": config.vector_store_id,
            "query": config.query,
            "search_query": getattr(results, "search_query", None),
            "has_more": getattr(results, "has_more", None),
            "next_page": getattr(results, "next_page", None),
            "results": compact_results,
            "raw": self._dump(results),
        }

    async def get_vector_store(self, config: OpenAIVectorStoreToolConfig) -> Dict[str, Any]:
        vector_store = await self._retrieve_vector_store_with_retry(vector_store_id=config.vector_store_id)

        return {
            "ok": True,
            "operation": config.operation.value,
            "vector_store": self._dump(vector_store),
        }

    async def list_vector_store_files(self, config: OpenAIVectorStoreToolConfig) -> Dict[str, Any]:
        paginator = await self._list_vector_store_files_with_retry(
            vector_store_id=config.vector_store_id,
            limit=config.limit,
        )

        items: List[Dict[str, Any]] = []
        async for item in paginator:
            items.append(self._dump(item))
            if len(items) >= config.limit:
                break

        return {
            "ok": True,
            "operation": config.operation.value,
            "vector_store_id": config.vector_store_id,
            "count": len(items),
            "files": items,
        }

    async def get_file_batch(self, config: OpenAIVectorStoreToolConfig) -> Dict[str, Any]:
        batch = await self._retrieve_file_batch_with_retry(
            vector_store_id=config.vector_store_id,
            batch_id=config.batch_id,
        )
        return {
            "ok": True,
            "operation": config.operation.value,
            "vector_store_id": config.vector_store_id,
            "batch": self._dump(batch),
        }

    async def delete_vector_store_files(self, config: OpenAIVectorStoreToolConfig) -> Dict[str, Any]:
        result = await self._delete_vector_store_files_internal(
            vector_store_id=config.vector_store_id,
            delete_file_ids=config.delete_file_ids,
            delete_underlying_openai_files=config.delete_underlying_openai_files,
        )

        attachment_errors = sum(
            1 for x in result.get("deleted_attachments", []) if "error" in x
        )
        openai_file_errors = sum(
            1 for x in result.get("deleted_openai_files", []) if "error" in x
        )
        total_errors = attachment_errors + openai_file_errors

        total_attempts = len(result.get("deleted_attachments", [])) + len(result.get("deleted_openai_files", []))
        successful_operations = total_attempts - total_errors

        ok = total_errors == 0
        partial_success = total_errors > 0 and successful_operations > 0

        return {
            "ok": ok,
            "partial_success": partial_success,
            "operation": config.operation.value,
            "vector_store_id": config.vector_store_id,
            **result,
        }

    async def delete_vector_store(self, config: OpenAIVectorStoreToolConfig) -> Dict[str, Any]:
        deleted = await self._delete_vector_store_with_retry(vector_store_id=config.vector_store_id)
        return {
            "ok": True,
            "operation": config.operation.value,
            "deleted": self._dump(deleted),
        }

    async def _delete_vector_store_files_internal(
        self,
        *,
        vector_store_id: str,
        delete_file_ids: List[str],
        delete_underlying_openai_files: bool,
    ) -> Dict[str, Any]:
        deleted_underlying_openai_files = []

        deleted_attachments = []

        for vector_store_file_id in delete_file_ids:
            vector_store_file_id = str(vector_store_file_id).strip()
            if not vector_store_file_id:
                continue

            try:
                res = await self._delete_vector_store_file_with_retry(
                    vector_store_id=vector_store_id,
                    file_id=vector_store_file_id,
                )
                dumped = self._dump(res)
                if isinstance(dumped, dict):
                    dumped.setdefault("vector_store_file_id", vector_store_file_id)
                deleted_attachments.append(dumped)
            except Exception as e:
                logger.bind(
                    ctx=self.ctx,
                    vector_store_file_id=vector_store_file_id,
                ).exception(f"Failed deleting vector_store.file attachment: {str(e)}")
                deleted_attachments.append(
                    {
                        "vector_store_file_id": vector_store_file_id,
                        "error": str(e),
                    }
                )

        return {
            "deleted_attachments": deleted_attachments,
            "deleted_openai_files": deleted_underlying_openai_files,
        }

    async def _resolve_openai_file_ids(
        self,
        *,
        files: List[OpenAIVectorStoreFileInput],
    ) -> Dict[str, Any]:
        url_items = [f for f in files if f.url]
        upload_result = {"uploaded_map": {}, "successful_files": [], "failed_files": []}

        if url_items:
            upload_result = await self._upload_urls_to_openai(
                urls=[f.url for f in url_items if f.url]
            )

        resolved: Dict[str, str] = {}
        failed_inputs: List[Dict[str, Any]] = list(upload_result["failed_files"])
        reused_files: List[Dict[str, Any]] = []

        for file_item in files:
            source_key = self._source_key(file_item)

            if file_item.openai_file_id:
                resolved[source_key] = file_item.openai_file_id
                reused_files.append({
                    "source": file_item.openai_file_id,
                    "status": "reused",
                    "openai_file_id": file_item.openai_file_id,
                })
                continue

            assert file_item.url is not None
            openai_file_id = upload_result["uploaded_map"].get(file_item.url)
            if openai_file_id:
                resolved[source_key] = openai_file_id

        return {
            "resolved_file_ids": resolved,
            "successful_files": upload_result["successful_files"],
            "reused_files": reused_files,
            "failed_files": failed_inputs,
        }

    async def _upload_single_url_with_outcome(
        self,
        *,
        url: str,
        semaphore: asyncio.Semaphore,
    ) -> UploadOutcome:
        tmpfiles: Dict[str, str] = {}
        try:
            async with semaphore:
                # per-URL handling so one bad URL does not kill the whole batch
                self._validate_supported_urls([
                    OpenAIVectorStoreFileInput(url=url)
                ])

                tmpfiles = await FileDownloader._download_urls_to_tmpfiles(
                    ctx=self.ctx,
                    user=self.user,
                    db=self.db,
                    urls=[url],
                    supported_file_formats=list(OPENAI_VECTOR_STORE_SUPPORTED_EXTENSIONS),
                )

                tmp_path = tmpfiles[url]
                openai_files = OpenaiFiles(openai_client=self.client)
                file_obj = await self._upload_single_tmpfile_to_openai(
                    openai_files=openai_files,
                    url=url,
                    tmp_path=tmp_path,
                )
                return {
                    "source": url,
                    "status": "uploaded",
                    "openai_file_id": file_obj.id,
                }

        except Exception as e:
            logger.bind(ctx=self.ctx, url=url).exception("Failed uploading file to OpenAI")
            return {
                "source": url,
                "status": "failed",
                "reason": str(e),
            }

        finally:
            for _, tmp_path in tmpfiles.items():
                if tmp_path and os.path.exists(tmp_path):
                    os.remove(tmp_path)

    async def _upload_urls_to_openai(
        self,
        *,
        urls: List[str],
    ) -> Dict[str, Any]:
        unique_urls = list(dict.fromkeys(urls))
        semaphore = asyncio.Semaphore(MAX_PARALLEL_UPLOADS)

        results = await asyncio.gather(*[
            self._upload_single_url_with_outcome(url=url, semaphore=semaphore)
            for url in unique_urls
        ])

        successes = [r for r in results if r["status"] == "uploaded"]
        failures = [r for r in results if r["status"] == "failed"]

        uploaded_map = {
            r["source"]: r["openai_file_id"]
            for r in successes
        }

        return {
            "uploaded_map": uploaded_map,
            "successful_files": successes,
            "failed_files": failures,
        }

    async def _poll_file_batch(
        self,
        *,
        vector_store_id: str,
        batch_id: str,
        poll_interval_sec: int,
        poll_timeout_sec: int,
    ) -> Any:
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        last_batch = None

        while True:
            last_batch = await self._retrieve_file_batch_with_retry(
                vector_store_id=vector_store_id,
                batch_id=batch_id,
            )
            status = getattr(last_batch, "status", None)

            if status in TERMINAL_BATCH_STATUSES:
                return last_batch

            elapsed = loop.time() - started_at
            if elapsed >= poll_timeout_sec:
                raise HTTPException(
                    status_code=504,
                    detail={
                        "message": "Timed out while waiting for vector store file batch",
                        "vector_store_id": vector_store_id,
                        "batch_id": batch_id,
                        "last_status": status,
                    },
                )

            await asyncio.sleep(poll_interval_sec)

    def _validate_supported_urls(self, files: List[OpenAIVectorStoreFileInput]) -> None:
        unsupported = []
        for file_item in files:
            assert file_item.url is not None
            ext = self._extract_extension_from_url(file_item.url)
            if not ext or ext not in OPENAI_VECTOR_STORE_SUPPORTED_EXTENSIONS:
                unsupported.append(
                    {
                        "url": file_item.url,
                        "extension": ext,
                    }
                )

        if unsupported:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Some files are not supported by OpenAI retrieval/vector stores",
                    "unsupported_files": unsupported,
                    "supported_extensions": sorted(OPENAI_VECTOR_STORE_SUPPORTED_EXTENSIONS),
                },
            )

    def _merge_attributes(
        self,
        batch_attributes: Dict[str, AttributeValue],
        file_attributes: Dict[str, AttributeValue],
    ) -> Dict[str, AttributeValue]:
        merged = {**batch_attributes, **file_attributes}
        self._validate_attributes(merged)
        return merged

    def _validate_attributes(self, attributes: Dict[str, AttributeValue]) -> None:
        if not attributes:
            return
        if len(attributes) > 16:
            raise HTTPException(
                status_code=400,
                detail="OpenAI vector store file attributes support at most 16 keys",
            )

        for key, value in attributes.items():
            if not isinstance(key, str):
                raise HTTPException(status_code=400, detail="Attribute keys must be strings")
            if len(key) > 64:
                raise HTTPException(
                    status_code=400,
                    detail=f"Attribute key too long: {key!r} (max 64 chars)",
                )

            if not isinstance(value, (str, int, float, bool)):
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported attribute value type for key {key!r}",
                )

            if isinstance(value, str) and len(value) > 512:
                raise HTTPException(
                    status_code=400,
                    detail=f"Attribute value too long for key {key!r} (max 512 chars)",
                )

    def _chunking_to_dict(
        self, chunking: OpenAIVectorStoreChunkingStrategy
    ) -> Dict[str, Any]:
        return chunking.to_openai_dict()

    async def _extract_file_name(self, *, url: str, tmp_path: str) -> str:
        try:
            file_name = await UserFiles.extract_file_name(ctx=self.ctx, file_url=url)
            if file_name:
                return file_name
        except Exception:
            pass

        parsed = urlparse(url)
        guessed = unquote(Path(parsed.path).name)
        if guessed:
            return guessed

        return Path(tmp_path).name

    def _extract_extension_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        path = unquote(parsed.path or "")
        return Path(path).suffix.lower()

    def _source_key(self, file_item: OpenAIVectorStoreFileInput) -> str:
        return file_item.openai_file_id or file_item.url or "unknown"

    def _notify(self, message: str) -> None:
        if self.notify:
            self.notify(self.ctx, message)

    def _dump(self, value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump(exclude_none=True)

        if isinstance(value, list):
            return [self._dump(v) for v in value]

        if isinstance(value, dict):
            return {k: self._dump(v) for k, v in value.items()}

        if hasattr(value, "model_dump"):
            try:
                return value.model_dump(exclude_none=True)
            except Exception:
                pass

        return value


async def _get_system_openai_for_subscriber(
    ctx: Context, db: AsyncIOMotorDatabase, user: UserORM
) -> OpenAI | None:
    """AUT-1459: system OpenAI fallback for product subscribers.

    Product subscribers run on system OpenRouter creds and typically have no
    personal OpenAI key, so OpenAI-backed file search fails for them. When the
    user has no resolvable OpenAI connection but IS a valid Platform/Kiwi-Code
    subscriber, use the platform (system) OpenAI key so file search works.

    Scoped to `openai_vector_store_tool` on purpose — it does NOT change OpenAI
    resolution for other flows. Returns None if the system key is unset or the
    user is not a subscriber (so the caller surfaces the original error).
    """
    settings = SettingsProvider.sget()
    if not settings.SUBSCRIBER_OPENAI_API_KEY:
        return None
    try:
        if not await UserAppConnRule._has_product_access(ctx=ctx, db=db, user=user):
            return None
    except Exception as e:
        logger.bind(ctx=ctx, error=str(e)).error(
            "Failed product-access check for OpenAI system fallback"
        )
        return None

    logger.bind(ctx=ctx).info("Using system OpenAI creds (product subscriber fallback)")
    return get_conn_openai(
        ctx=ctx,
        api_key=settings.SUBSCRIBER_OPENAI_API_KEY,
        org_id=settings.SUBSCRIBER_OPENAI_ORG_ID,
    )


async def openai_vector_store_tool(
    config: OpenAIVectorStoreToolConfig,
    ctx: Context,
    app_state: AppState,
    user: UserORM,
    db: AsyncIOMotorDatabase,
    notify: Notify | None = None,
) -> DataBlocks | Exception:
    """
    Description
    -----------
    Provides a standalone OpenAI Vector Store management and retrieval tool.
    This tool talks directly to OpenAI Files and Vector Stores APIs and is
    designed to be reused by any LLM workflow that needs first-class file
    indexing and retrieval capabilities.

    Supported Operations
    --------------------
    The tool supports the following operations through
    `OpenAIVectorStoreToolConfig.operation`:

    1. create_vector_store
       Create a new OpenAI vector store with optional metadata and expiration settings.

    2. add_files
       Upload supported files and attach them to an existing vector store for indexing.
       Supports per-file attributes, optional chunking configuration, and optional polling
       until indexing is completed.

    3. reindex_files
       Re-index content in an existing vector store by deleting selected vector-store file
       attachments and/or re-adding files. This is useful when the caller wants explicit
       control over replacement or refresh behavior.

    4. query_vector_store
       Perform direct retrieval against an existing vector store using OpenAI's vector-store
       search API. This is intended for standalone search use cases where another LLM will
       consume the retrieved results.

    5. get_vector_store
       Retrieve metadata and current state of a vector store.

    6. list_vector_store_files
       List files currently attached to a vector store.

    7. get_file_batch
       Retrieve the status and details of a vector-store file batch.

    8. delete_vector_store_files
       Delete one or more file attachments from a vector store. Optionally also delete the
       underlying OpenAI files when explicitly requested.

    9. delete_vector_store
       Delete an entire vector store.

    Input
    -----
    config : OpenAIVectorStoreToolConfig
        Structured configuration describing the requested operation and its arguments.

        Depending on the selected operation, the config may include:
        - operation: str
            Use any one from these according to task ( create_vector_store , add_files , reindex_files , query_vector_store , get_vector_store , list_vector_store_files , get_file_batch , delete_vector_store_files , delete_vector_store ) and this required param.
        - vector_store_id : str
            Existing OpenAI vector store ID for operations that target a store.
        - name : str
            Name of a new vector store to create.
        - metadata : dict
            Optional metadata to associate with the vector store.
        - expires_after_days : int
            Optional expiration policy for new vector stores.
        - files : list[OpenAIVectorStoreFileInput]
            Files to upload or attach. Each item may reference either:
              - a file URL, or
              - an existing OpenAI file ID
        - batch_attributes : dict
            Attributes applied to all files in the batch unless overridden per file.
        - batch_chunking_strategy : OpenAIVectorStoreChunkingStrategy
            Optional shared chunking strategy for batch ingestion.
        - wait_for_completion : bool
            Whether to poll until indexing is finished.
        - poll_interval_sec : int
            Polling interval for file batch status checks.
        - poll_timeout_sec : int
            Timeout for polling ingestion status.
        - delete_file_ids : list[str]
            Vector-store file attachment IDs to remove from the vector store during delete or re-index operations.        
        - delete_underlying_openai_files : bool
            Whether to also delete the underlying OpenAI files.
        - query : str
            Query string for direct vector-store retrieval.
        - max_num_results : int
            Maximum number of search results to return.
        - rewrite_query : bool
            Whether query rewriting should be enabled during search.
        - filters : dict
            Optional structured filter object for narrowing retrieval results.
        - ranking_options : dict
            Optional ranking configuration for search.
        - batch_id : str
            Batch ID for retrieving file-batch status.
        - limit : int
            Maximum number of files to list.

    File Handling
    -------------
    This tool validates file support before uploading or indexing.
    Only files supported by OpenAI retrieval/vector-store ingestion should be passed.

    For URL-based files, the tool:
    - downloads the file to a temporary local path
    - validates its extension
    - uploads it to OpenAI Files
    - attaches it to the requested vector store

    For `openai_file_id` inputs, the tool skips upload and directly uses the
    already-existing OpenAI file.
    """
    try:
        openai_conn = await AppConn.get_openai(
            ctx=ctx,
            db=db,
            user=user,
            provider="openai",
        )
        # AppConn.get_openai returns (not raises) the exception on failure — e.g.
        # a system-creds/OpenRouter product-subscriber run that has no OpenAI
        # credentials.
        if isinstance(openai_conn, Exception):
            # AUT-1459: enable file search for product subscribers who have no
            # personal OpenAI key by falling back to the system OpenAI connection
            # (env var). Scoped to this tool on purpose. Surface the original
            # error cleanly (not an AttributeError on `.client`) if not eligible.
            resolution_error = openai_conn
            openai_conn = await _get_system_openai_for_subscriber(ctx=ctx, db=db, user=user)
            if openai_conn is None:
                raise resolution_error

        client: AsyncOpenAI = openai_conn.client
        logger.info(f"provider={openai_conn.provider} client_type={type(client)} base_url={getattr(client,'base_url',None)} openai_api_type={getattr(client,'api_type',None)}")


        service = OpenAIVectorStoreToolService(
            client=client,
            ctx=ctx,
            user=user,
            db=db,
            notify=notify,
        )
        result = await service.execute(config=config)

        data_block = DataBlock(text=json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return DataBlocks(blocks=[data_block])

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in openai_vector_store_tool: {str(e)}")
        return e