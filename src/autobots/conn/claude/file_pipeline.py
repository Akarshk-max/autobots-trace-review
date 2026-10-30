import hashlib
import mimetypes
import os
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from anthropic import AsyncAnthropic
from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, ConfigDict, Field

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_text2text.action_convert_file_to_pdf_or_txt import (
    ActionText2TextConvertToPDForText,
    ConvertConfig,
)
from src.autobots.conn.claude.model_registry import ClaudeModelRegistry, ClaudeProvider
from src.autobots.data_model.context import Context
from src.autobots.files.file_downloader import FileDownloader
from src.autobots.user.user_orm_model import UserORM


class ClaudeFileKind(str, Enum):
    PDF = "pdf"
    TEXT = "text"
    IMAGE = "image"
    TABULAR = "tabular"
    STRUCTURED = "structured"
    OFFICE = "office"
    OTHER = "other"


class ClaudeFileDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: str
    local_path: str
    extension: str
    mime_type: str | None = None
    kind: ClaudeFileKind
    content_sha256: str | None = None
    size_bytes: int | None = None

class ClaudeFilePipelineResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Safe to place inside tool_result.content OR normal messages
    blocks: list[dict[str, Any]] = Field(default_factory=list)

    # Must NOT go inside tool_result.content.
    # These must be sibling content blocks in a user message.
    container_upload_blocks: list[dict[str, Any]] = Field(default_factory=list)

    required_betas: list[str] = Field(default_factory=list)
    uploaded_file_ids: list[str] = Field(default_factory=list)
    uploaded_files: list[dict[str, Any]] = Field(default_factory=list)
    failed_urls: list[dict[str, Any]] = Field(default_factory=list)

class ClaudeFilePipeline:
    SUPPORTED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}
    TABULAR_EXTENSIONS = {"csv", "xlsx", "xls", "tsv"}
    TEXT_EXTENSIONS = {"txt", "md", "rst", "log"}
    STRUCTURED_EXTENSIONS = {"json", "jsonl", "xml"}
    OFFICE_EXTENSIONS = {"docx", "html", "htm"}
    MAX_INLINE_TEXT_CHARS = 20_000
    MAX_INLINE_PREVIEW_LINES = 40

    def __init__(
        self,
        client: AsyncAnthropic,
        provider: ClaudeProvider = ClaudeProvider.ANTHROPIC,
    ) -> None:
        self.client = client
        self.provider = provider


    @staticmethod
    def _sha256_file(path: str) -> str:
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    @staticmethod
    def _display_file_name_from_url(url: str, fallback_path: str) -> str:
        parsed = urlparse(str(url or ""))
        guessed = unquote(Path(parsed.path or "").name)
        return guessed or os.path.basename(fallback_path)

    @staticmethod
    def _find_reusable_uploaded_file_id(
        *,
        existing_uploaded_files_manifest: list[dict[str, Any]] | None,
        descriptor: ClaudeFileDescriptor,
    ) -> str | None:
        target_url = str(descriptor.source_url or "").strip()
        target_sha = str(descriptor.content_sha256 or "").strip()
        if not target_url or not target_sha:
            return None

        for raw_item in existing_uploaded_files_manifest or []:
            item = dict(raw_item or {})
            source_url = str(item.get("source_url") or item.get("s3_url") or "").strip()
            content_sha256 = str(item.get("content_sha256") or "").strip()

            if source_url != target_url:
                continue
            if content_sha256 != target_sha:
                continue

            claude_file_id = str(item.get("claude_file_id") or item.get("file_id") or "").strip()
            if claude_file_id:
                return claude_file_id

        return None

    @staticmethod
    def _build_cached_document_block(
        file_id: str,
        citations_enabled: bool,
    ) -> dict[str, Any]:
        return {
            "type": "document",
            "source": {"type": "file", "file_id": file_id},
            "citations": {"enabled": citations_enabled},
        }

    @staticmethod
    def _build_cached_image_block(
        file_id: str,
    ) -> dict[str, Any]:
        return {
            "type": "image",
            "source": {"type": "file", "file_id": file_id},
        }
    
    @staticmethod
    def _append_unique_uploaded_file_id(
        result: ClaudeFilePipelineResult,
        file_id: str,
    ) -> None:
        if file_id not in result.uploaded_file_ids:
            result.uploaded_file_ids.append(file_id)

    @staticmethod
    def _record_uploaded_file(
        result: ClaudeFilePipelineResult,
        *,
        descriptor: ClaudeFileDescriptor,
        file_id: str,
        reused: bool = False,
    ) -> None:
        result.uploaded_files.append(
            {
                "source_url": descriptor.source_url,
                "s3_url": descriptor.source_url,
                "file_id": file_id,
                "file_name": ClaudeFilePipeline._display_file_name_from_url(
                    descriptor.source_url,
                    descriptor.local_path,
                ),
                "content_sha256": descriptor.content_sha256,
                "size_bytes": descriptor.size_bytes,
                "reused": reused,
            }
        )

    @staticmethod
    def _record_failed_url(
        result: ClaudeFilePipelineResult,
        *,
        url: str,
        stage: str,
        exc: Exception,
        descriptor: ClaudeFileDescriptor | None = None,
    ) -> None:
        error_type = exc.__class__.__name__
        status_code = getattr(exc, "status_code", None)

        if isinstance(exc, HTTPException):
            error_message = str(exc.detail)
            status_code = exc.status_code
        else:
            error_message = str(exc)

        failed_item: dict[str, Any] = {
            "source_url": str(url),
            "stage": str(stage or "unknown"),
            "error_type": error_type,
            "error": error_message,
        }

        if status_code is not None:
            failed_item["status_code"] = status_code

        if descriptor is not None:
            failed_item["file_name"] = ClaudeFilePipeline._display_file_name_from_url(
                descriptor.source_url,
                descriptor.local_path,
            )
            failed_item["extension"] = descriptor.extension
            failed_item["mime_type"] = descriptor.mime_type
            failed_item["kind"] = descriptor.kind.value
            failed_item["content_sha256"] = descriptor.content_sha256
            failed_item["size_bytes"] = descriptor.size_bytes

        result.failed_urls.append(failed_item)

    @classmethod
    def _read_text_file_limited(cls, path: str) -> str:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
        if len(text) <= cls.MAX_INLINE_TEXT_CHARS:
            limited = text
        else:
            limited = text[: cls.MAX_INLINE_TEXT_CHARS] + "\n\n...[truncated by ClaudeFilePipeline]"

        lines = limited.splitlines()[: cls.MAX_INLINE_PREVIEW_LINES]
        return "\n".join(lines)

    @classmethod
    def _build_file_preview_block(
        cls,
        descriptor: ClaudeFileDescriptor,
        *,
        label: str,
    ) -> dict[str, Any]:
        preview = cls._read_text_file_limited(descriptor.local_path)
        return {
            "type": "text",
            "text": (
                f"{label}: {os.path.basename(descriptor.local_path)}\n"
                f"(preview only; full file not inlined)\n\n"
                f"{preview}"
            ),
        }

    @staticmethod
    def _is_directly_readable_text(descriptor: ClaudeFileDescriptor) -> bool:
        if descriptor.kind == ClaudeFileKind.TEXT:
            return True
        return descriptor.extension.lower() in {"csv", "tsv", "json", "jsonl", "xml"}

    async def _build_non_files_api_text_block(
        self,
        *,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase | None,
        descriptor: ClaudeFileDescriptor,
    ) -> dict[str, Any]:
        if self._is_directly_readable_text(descriptor):
            return self._build_file_preview_block(
                descriptor,
                label="Directly readable file preview",
            )

        converted_url = await self._convert_to_pdf_or_text(
            ctx=ctx,
            user=user,
            db=db,
            url=descriptor.source_url,
        )
        converted_descriptor = await self._download_descriptor(
            ctx=ctx,
            user=user,
            db=db,
            url=converted_url,
        )

        if self._is_directly_readable_text(converted_descriptor):
            return self._build_file_preview_block(
                converted_descriptor,
                label="Converted file preview",
            )

        raise HTTPException(
            status_code=415,
            detail=(
                "File could not be converted into a text-compatible representation "
                f"for provider {self.provider.value}: {descriptor.source_url}"
            ),
        )

    @staticmethod
    def _guess_mime_from_url(url: str) -> str | None:
        clean = url.split("?")[0].split("#")[0]
        mime, _ = mimetypes.guess_type(clean)
        return mime

    @classmethod
    def _detect_kind(cls, ext: str) -> ClaudeFileKind:
        ext = ext.lower().lstrip(".")
        if ext == "pdf":
            return ClaudeFileKind.PDF
        if ext in cls.SUPPORTED_IMAGE_EXTENSIONS:
            return ClaudeFileKind.IMAGE
        if ext in cls.TEXT_EXTENSIONS:
            return ClaudeFileKind.TEXT
        if ext in cls.TABULAR_EXTENSIONS:
            return ClaudeFileKind.TABULAR
        if ext in cls.STRUCTURED_EXTENSIONS:
            return ClaudeFileKind.STRUCTURED
        if ext in cls.OFFICE_EXTENSIONS:
            return ClaudeFileKind.OFFICE
        return ClaudeFileKind.OTHER

    @staticmethod
    def _has_code_execution(tools: list[Any] | None) -> bool:
        if not tools:
            return False

        for tool in tools:
            if isinstance(tool, str) and tool == "code_interpreter":
                return True

            if isinstance(tool, dict):
                tool_type = tool.get("type")
                tool_name = tool.get("name")
                if tool_type in {"code_execution_20250522", "code_execution_20250825", "code_execution_20260120"}:
                    return True
                if tool_name == "code_execution":
                    return True

            if getattr(tool, "type", None) in {
                "code_execution_20250522",
                "code_execution_20250825",
                "code_execution_20260120",
            }:
                return True

            if getattr(tool, "name", None) == "code_execution":
                return True

        return False

    async def _download_descriptor(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase | None,
        url: str,
    ) -> ClaudeFileDescriptor:
        downloaded_files = await FileDownloader._download_urls_to_tmpfiles(
            ctx=ctx,
            user=user,
            db=db,
            urls=[url],
        )
        local_path = downloaded_files.get(url)
        if not local_path:
            raise HTTPException(status_code=500, detail=f"Failed to download file: {url}")

        local = Path(local_path)
        ext = local.suffix.lower().lstrip(".")
        mime_type = self._guess_mime_from_url(url) or mimetypes.guess_type(str(local))[0]

        return ClaudeFileDescriptor(
            source_url=url,
            local_path=str(local),
            extension=ext,
            mime_type=mime_type,
            kind=self._detect_kind(ext),
            content_sha256=self._sha256_file(str(local)),
            size_bytes=local.stat().st_size,
        )
    
    async def _upload_raw_file(self, descriptor: ClaudeFileDescriptor) -> str:
        with open(descriptor.local_path, "rb") as file_obj:
            uploaded = await self.client.beta.files.upload(
                file=(
                    os.path.basename(descriptor.local_path),
                    file_obj,
                    descriptor.mime_type or "application/octet-stream",
                )
            )
        return uploaded.id

    async def _upload_pdf_document(
        self,
        descriptor: ClaudeFileDescriptor,
        citations_enabled: bool,
    ) -> tuple[dict[str, Any], str]:
        with open(descriptor.local_path, "rb") as file_handle:
            file_obj = await self.client.beta.files.upload(
                file=(
                    os.path.basename(descriptor.local_path),
                    file_handle,
                    "application/pdf",
                )
            )

        block = {
            "type": "document",
            "source": {"type": "file", "file_id": file_obj.id},
            "citations": {"enabled": citations_enabled},
        }
        return block, file_obj.id

    async def _upload_text_document(
        self,
        descriptor: ClaudeFileDescriptor,
        citations_enabled: bool,
    ) -> tuple[dict[str, Any], str]:
        with open(descriptor.local_path, "rb") as file_handle:
            file_obj = await self.client.beta.files.upload(
                file=(
                    os.path.basename(descriptor.local_path),
                    file_handle,
                    descriptor.mime_type or "text/plain",
                )
            )

        block = {
            "type": "document",
            "source": {"type": "file", "file_id": file_obj.id},
            "citations": {"enabled": citations_enabled},
        }
        return block, file_obj.id

    async def _upload_image_block(
        self,
        descriptor: ClaudeFileDescriptor,
    ) -> tuple[dict[str, Any], str]:
        media = descriptor.mime_type or "image/png"
        with open(descriptor.local_path, "rb") as file_handle:
            file_obj = await self.client.beta.files.upload(
                file=(
                    os.path.basename(descriptor.local_path),
                    file_handle,
                    media,
                )
            )

        block = {
            "type": "image",
            "source": {"type": "file", "file_id": file_obj.id},
        }
        return block, file_obj.id

    async def _convert_to_pdf_or_text(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase | None,
        url: str,
    ) -> str:
        action_input = DataBlock(urls=[url])
        action = ActionText2TextConvertToPDForText(
            action_config=ConvertConfig(url=""),
            user=user,
            db=db,
        )
        result = await action.run_action(ctx=ctx, action_input=action_input)

        if isinstance(result, Exception):
            raise result

        if not result.blocks or not result.blocks[0].urls:
            raise HTTPException(status_code=500, detail=f"Conversion failed for file: {url}")

        return result.blocks[0].urls[0]

    def _add_container_upload(
        self,
        result: ClaudeFilePipelineResult,
        file_id: str,
    ) -> None:
        result.container_upload_blocks.append(
            {
                "type": "container_upload",
                "file_id": file_id,
            }
        )

    async def build_blocks(
        self,
        *,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase | None,
        urls: list[str],
        tools: list[Any] | None,
        model: str,
        citations_enabled: bool,
        existing_uploaded_files_manifest: list[dict[str, Any]] | None = None,
    ) -> ClaudeFilePipelineResult:
        result = ClaudeFilePipelineResult()
        if not urls:
            return result

        seen_urls: set[str] = set()
        normalized_urls: list[str] = []
        for raw_url in urls:
            url = str(raw_url or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            normalized_urls.append(url)

        capabilities = ClaudeModelRegistry.resolve(model, self.provider).capabilities
        has_code_execution = self._has_code_execution(tools)

        attach_only_to_code_execution = bool(
            capabilities.supports_files_api
            and has_code_execution
            and capabilities.supports_code_execution
        )

        if capabilities.supports_files_api:
            result.required_betas.append("files-api-2025-04-14")

        for url in normalized_urls:
            descriptor: ClaudeFileDescriptor | None = None
            stage = "init"

            try:
                descriptor = await self._download_descriptor(ctx=ctx, user=user, db=db, url=url)
                stage = "download_descriptor"

                cached_file_id = str(
                    self._find_reusable_uploaded_file_id(
                        existing_uploaded_files_manifest=existing_uploaded_files_manifest,
                        descriptor=descriptor,
                    )
                    or ""
                ).strip()

                logger.info(
                    "Claude file pipeline url={} ext={} kind={} provider={} code_execution={} "
                    "attach_only_to_code_execution={} cache_hit={}",
                    url,
                    descriptor.extension,
                    descriptor.kind.value,
                    self.provider.value,
                    has_code_execution,
                    attach_only_to_code_execution,
                    bool(cached_file_id),
                )

                if not capabilities.supports_files_api:
                    stage = "build_non_files_api_text_block"
                    result.blocks.append(
                        await self._build_non_files_api_text_block(
                            ctx=ctx,
                            user=user,
                            db=db,
                            descriptor=descriptor,
                        )
                    )
                    continue

                # Reuse already-uploaded Claude file IDs whenever possible.
                if cached_file_id:
                    if attach_only_to_code_execution:
                        stage = "reuse_cached_file_id_for_container_upload"
                        self._add_container_upload(result, cached_file_id)
                        self._append_unique_uploaded_file_id(result, cached_file_id)
                        self._record_uploaded_file(
                            result=result,
                            descriptor=descriptor,
                            file_id=cached_file_id,
                            reused=True,
                        )
                        continue

                    if descriptor.kind == ClaudeFileKind.PDF:
                        stage = "reuse_cached_pdf_document"
                        result.blocks.append(
                            self._build_cached_document_block(
                                file_id=cached_file_id,
                                citations_enabled=citations_enabled,
                            )
                        )
                        self._append_unique_uploaded_file_id(result, cached_file_id)
                        self._record_uploaded_file(
                            result=result,
                            descriptor=descriptor,
                            file_id=cached_file_id,
                            reused=True,
                        )
                        continue

                    if descriptor.kind == ClaudeFileKind.IMAGE:
                        stage = "reuse_cached_image_block"
                        result.blocks.append(self._build_cached_image_block(cached_file_id))
                        self._append_unique_uploaded_file_id(result, cached_file_id)
                        self._record_uploaded_file(
                            result=result,
                            descriptor=descriptor,
                            file_id=cached_file_id,
                            reused=True,
                        )
                        continue

                    if descriptor.kind == ClaudeFileKind.TEXT:
                        stage = "reuse_cached_text_document"
                        result.blocks.append(
                            self._build_cached_document_block(
                                file_id=cached_file_id,
                                citations_enabled=citations_enabled,
                            )
                        )
                        self._append_unique_uploaded_file_id(result, cached_file_id)
                        self._record_uploaded_file(
                            result=result,
                            descriptor=descriptor,
                            file_id=cached_file_id,
                            reused=True,
                        )
                        continue

                if attach_only_to_code_execution:
                    stage = "upload_raw_file_code_execution_only"
                    raw_file_id = await self._upload_raw_file(descriptor)
                    self._add_container_upload(result, raw_file_id)
                    self._append_unique_uploaded_file_id(result, raw_file_id)
                    self._record_uploaded_file(
                        result=result,
                        descriptor=descriptor,
                        file_id=raw_file_id,
                        reused=False,
                    )
                    continue

                if descriptor.kind == ClaudeFileKind.PDF:
                    stage = "upload_pdf_document"
                    block, file_id = await self._upload_pdf_document(descriptor, citations_enabled)
                    result.blocks.append(block)
                    self._append_unique_uploaded_file_id(result, file_id)
                    self._record_uploaded_file(result=result, descriptor=descriptor, file_id=file_id)
                    continue

                if descriptor.kind == ClaudeFileKind.IMAGE:
                    stage = "upload_image_block"
                    block, file_id = await self._upload_image_block(descriptor)
                    result.blocks.append(block)
                    self._append_unique_uploaded_file_id(result, file_id)
                    self._record_uploaded_file(result=result, descriptor=descriptor, file_id=file_id)
                    continue

                if descriptor.kind == ClaudeFileKind.TEXT:
                    stage = "upload_text_document"
                    block, file_id = await self._upload_text_document(descriptor, citations_enabled)
                    result.blocks.append(block)
                    self._append_unique_uploaded_file_id(result, file_id)
                    self._record_uploaded_file(result=result, descriptor=descriptor, file_id=file_id)
                    continue

                if descriptor.kind in {ClaudeFileKind.TABULAR, ClaudeFileKind.STRUCTURED}:
                    if has_code_execution:
                        stage = "upload_raw_file_code_execution_tabular_or_structured"
                        raw_file_id = await self._upload_raw_file(descriptor)
                        self._add_container_upload(result, raw_file_id)
                        self._append_unique_uploaded_file_id(result, raw_file_id)
                        self._record_uploaded_file(
                            result=result,
                            descriptor=descriptor,
                            file_id=raw_file_id,
                        )
                        continue

                    stage = "convert_tabular_or_structured_to_text"
                    converted_url = await self._convert_to_pdf_or_text(
                        ctx=ctx,
                        user=user,
                        db=db,
                        url=url,
                    )

                    stage = "download_converted_tabular_or_structured"
                    converted_descriptor = await self._download_descriptor(
                        ctx=ctx,
                        user=user,
                        db=db,
                        url=converted_url,
                    )

                    if converted_descriptor.kind == ClaudeFileKind.TEXT:
                        stage = "upload_converted_tabular_or_structured_text"
                        block, file_id = await self._upload_text_document(
                            converted_descriptor,
                            citations_enabled,
                        )
                        result.blocks.append(block)
                        self._append_unique_uploaded_file_id(result, file_id)
                        self._record_uploaded_file(
                            result=result,
                            descriptor=converted_descriptor,
                            file_id=file_id,
                        )
                        continue

                    stage = "build_converted_tabular_or_structured_preview"
                    result.blocks.append(
                        self._build_file_preview_block(
                            converted_descriptor,
                            label="Converted file preview",
                        )
                    )
                    continue

                if descriptor.kind in {ClaudeFileKind.OFFICE, ClaudeFileKind.OTHER}:
                    stage = "convert_to_pdf_or_text"
                    converted_url = await self._convert_to_pdf_or_text(
                        ctx=ctx,
                        user=user,
                        db=db,
                        url=url,
                    )

                    stage = "download_converted_descriptor"
                    converted_descriptor = await self._download_descriptor(
                        ctx=ctx,
                        user=user,
                        db=db,
                        url=converted_url,
                    )

                    if converted_descriptor.kind == ClaudeFileKind.PDF:
                        stage = "upload_converted_pdf_document"
                        block, file_id = await self._upload_pdf_document(
                            converted_descriptor,
                            citations_enabled,
                        )
                        result.blocks.append(block)
                        self._append_unique_uploaded_file_id(result, file_id)
                        self._record_uploaded_file(
                            result=result,
                            descriptor=converted_descriptor,
                            file_id=file_id,
                        )
                        continue

                    if converted_descriptor.kind == ClaudeFileKind.TEXT:
                        stage = "upload_converted_text_document"
                        block, file_id = await self._upload_text_document(
                            converted_descriptor,
                            citations_enabled,
                        )
                        result.blocks.append(block)
                        self._append_unique_uploaded_file_id(result, file_id)
                        self._record_uploaded_file(
                            result=result,
                            descriptor=converted_descriptor,
                            file_id=file_id,
                        )
                        continue

                    stage = "build_converted_preview"
                    result.blocks.append(
                        self._build_file_preview_block(
                            converted_descriptor,
                            label="Converted file preview",
                        )
                    )
                    continue

            except Exception as exc:
                logger.exception("Claude file pipeline failed url={} error={}", url, exc)
                self._record_failed_url(
                    result=result,
                    url=url,
                    stage=stage,
                    exc=exc,
                    descriptor=descriptor,
                )
                continue

        result.required_betas = list(dict.fromkeys(result.required_betas))
        return result