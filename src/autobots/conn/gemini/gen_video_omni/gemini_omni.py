import base64
import binascii
import mimetypes
import os
import re
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import anyio
import httpx
from fastapi import UploadFile
from google.genai import types
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.gemini.gemini_client import GeminiClient, get_gemini_client
from src.autobots.conn.gemini.gen_video_omni.gemini_omni_models import (
    OmniErrorPayload,
    OmniGeneratedVideo,
    OmniMediaInput,
    OmniVideoException,
    OmniVideoGenerateConfig,
    OmniVideoRequest,
    OmniVideoResult,
    is_retryable_exception,
)
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM

PENDING_INTERACTION_STATUSES = {"queued", "in_progress"}
COMPLETED_INTERACTION_STATUS = "completed"
FILE_ID_PATTERN = re.compile(r"files/([a-z0-9]+)")


def _default_timeout() -> httpx.Timeout:
    return httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)


class OmniVideoClient:
    def __init__(
        self,
        api_key: str,
        *,
        gemini_client: Optional[GeminiClient] = None,
        download_timeout: Optional[httpx.Timeout] = None,
        download_max_bytes: int = 512 * 1024 * 1024,
        poll_interval_seconds: float = 5.0,
    ):
        self._gemini = gemini_client or get_gemini_client(api_key=api_key)
        self._aclient = self._gemini.aclient
        self._download_timeout = download_timeout or _default_timeout()
        self._download_max_bytes = int(download_max_bytes)
        self._poll_interval_seconds = float(poll_interval_seconds)

    async def aclose(self) -> None:
        await self._gemini.aclose()

    def _enforce_max_bytes(self, n: int, *, where: str) -> None:
        if n > self._download_max_bytes:
            raise OmniVideoException(
                OmniErrorPayload(
                    code="MEDIA_TOO_LARGE",
                    message=f"{where} exceeds max bytes limit ({n} > {self._download_max_bytes})",
                    retryable=False,
                    details={"max_bytes": self._download_max_bytes, "got_bytes": n},
                )
            )

    @staticmethod
    def _normalize_mime(mime: Optional[str]) -> Optional[str]:
        if not mime:
            return None
        return mime.split(";")[0].strip().lower()

    @staticmethod
    def _infer_name_from_url(url: str) -> str:
        tail = url.split("?")[0].rstrip("/").split("/")[-1].strip()
        return tail or "media"

    async def _read_file_bytes(self, path: str) -> bytes:
        p = Path(path)
        if not p.exists():
            raise OmniVideoException(
                OmniErrorPayload(
                    code="MEDIA_INPUT_FILE_NOT_FOUND",
                    message=f"file_path not found: {path}",
                    retryable=False,
                    details={"file_path": path},
                )
            )
        data: bytes = await anyio.to_thread.run_sync(p.read_bytes)
        self._enforce_max_bytes(len(data), where="file_path")
        return data

    async def _download_url_bytes(self, url: str, http: httpx.AsyncClient) -> Tuple[bytes, httpx.Response]:
        try:
            async with http.stream("GET", url, follow_redirects=True) as r:
                r.raise_for_status()
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf.extend(chunk)
                    self._enforce_max_bytes(len(buf), where="url download")
                return bytes(buf), r
        except OmniVideoException:
            raise
        except Exception as e:
            raise OmniVideoException(
                OmniErrorPayload(
                    code="MEDIA_URL_DOWNLOAD_FAILED",
                    message=str(e),
                    retryable=is_retryable_exception(e),
                    details={"url": url},
                )
            ) from e

    async def _read_media_bytes(self, media: OmniMediaInput, http: httpx.AsyncClient) -> Tuple[bytes, str, str]:
        if media.file_path:
            data = await self._read_file_bytes(media.file_path)
            file_name = media.file_name or os.path.basename(media.file_path)
            mime_type = media.mime_type or mimetypes.guess_type(file_name)[0]
            return data, self._normalize_mime(mime_type) or "application/octet-stream", file_name

        if media.url:
            data, r = await self._download_url_bytes(str(media.url), http)
            file_name = media.file_name or self._infer_name_from_url(str(media.url))
            mime_type = media.mime_type or r.headers.get("content-type") or mimetypes.guess_type(file_name)[0]
            return data, self._normalize_mime(mime_type) or "application/octet-stream", file_name

        if media.base64_content:
            raw = media.base64_content.strip()
            mime_type = media.mime_type
            b64 = raw
            if raw.startswith("data:") and "base64," in raw:
                header, b64 = raw.split("base64,", 1)
                mime_type = mime_type or header.replace("data:", "").replace(";", "").strip() or None
            try:
                data = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError) as e:
                raise OmniVideoException(
                    OmniErrorPayload(
                        code="MEDIA_BASE64_INVALID",
                        message=f"Invalid base64_content: {e}",
                        retryable=False,
                    )
                ) from e
            self._enforce_max_bytes(len(data), where="base64_content")
            mime_type = self._normalize_mime(mime_type) or "application/octet-stream"
            file_name = media.file_name or "media"
            if "." not in file_name:
                file_name = f"{file_name}{mimetypes.guess_extension(mime_type) or ''}"
            return data, mime_type, file_name

        raise OmniVideoException(
            OmniErrorPayload(
                code="MEDIA_INPUT_INVALID",
                message="Invalid media input (no bytes source).",
                retryable=False,
            )
        )

    def _validate_mime_prefix(self, mime: str, *, prefix: str, where: str) -> None:
        if mime == "application/octet-stream" or mime.startswith(prefix):
            return
        raise OmniVideoException(
            OmniErrorPayload(
                code="MEDIA_MIME_MISMATCH",
                message=f"{where} mime_type is not {prefix}*: {mime}",
                retryable=False,
                details={"mime_type": mime},
            )
        )

    async def _to_image_content(self, media: OmniMediaInput, http: httpx.AsyncClient) -> Dict[str, Any]:
        if media.gemini_uri:
            return {"type": "image", "uri": str(media.gemini_uri)}
        data, mime, _name = await self._read_media_bytes(media, http)
        self._validate_mime_prefix(mime, prefix="image/", where="image")
        content: Dict[str, Any] = {"type": "image", "data": base64.b64encode(data).decode("ascii")}
        if mime != "application/octet-stream":
            content["mime_type"] = mime
        return content

    async def _to_video_content(self, media: OmniMediaInput, http: httpx.AsyncClient) -> Dict[str, Any]:
        if media.gemini_uri:
            return {"type": "video", "uri": str(media.gemini_uri)}
        data, mime, name = await self._read_media_bytes(media, http)
        self._validate_mime_prefix(mime, prefix="video/", where="video")
        upload_config = types.UploadFileConfig(
            mime_type=mime if mime != "application/octet-stream" else None,
            display_name=name,
        )
        uploaded = await self._aclient.files.upload(file=BytesIO(data), config=upload_config)
        uploaded = await self._gemini.wait_for_file_active(name=uploaded.name)
        return {"type": "video", "uri": uploaded.uri}

    async def _build_input(self, req: OmniVideoRequest, http: httpx.AsyncClient) -> List[Dict[str, Any]]:
        contents: List[Dict[str, Any]] = []
        if req.video:
            contents.append(await self._to_video_content(req.video, http))
        if req.image:
            contents.append(await self._to_image_content(req.image, http))
        contents.append({"type": "text", "text": req.prompt})
        return contents

    @staticmethod
    def _build_response_format(cfg: OmniVideoGenerateConfig) -> Dict[str, Any]:
        response_format: Dict[str, Any] = {"type": "video", "delivery": "uri"}
        if cfg.aspect_ratio:
            response_format["aspect_ratio"] = cfg.aspect_ratio.value
        if cfg.resolution:
            response_format["resolution"] = cfg.resolution.value
        if cfg.duration_seconds:
            response_format["duration"] = f"{cfg.duration_seconds}s"
        return response_format

    @staticmethod
    def _build_generation_config(cfg: OmniVideoGenerateConfig) -> Optional[Dict[str, Any]]:
        if cfg.task is None:
            return None
        return {"video_config": {"task": cfg.task.value}}

    def _build_create_kwargs(self, req: OmniVideoRequest, contents: List[Dict[str, Any]]) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": req.model.value,
            "input": contents,
            "response_format": self._build_response_format(req.generation_config),
            "timeout": req.generation_config.request_timeout_seconds,
        }
        generation_config = self._build_generation_config(req.generation_config)
        if generation_config:
            kwargs["generation_config"] = generation_config
        if req.previous_interaction_id:
            kwargs["previous_interaction_id"] = req.previous_interaction_id
        return kwargs

    async def _wait_for_completion(self, interaction: Any, *, timeout_seconds: float, ctx: Context, notify: Notify) -> Any:
        started = time.time()
        while str(getattr(interaction, "status", "")) in PENDING_INTERACTION_STATUSES:
            if (time.time() - started) > timeout_seconds:
                raise OmniVideoException(
                    OmniErrorPayload(
                        code="OMNI_POLL_TIMEOUT",
                        message=f"Timed out waiting for video generation (>{timeout_seconds}s)",
                        retryable=True,
                        details={"interaction_id": getattr(interaction, "id", None)},
                    )
                )
            self._notify(ctx, notify, f"Video generation time elapsed is {int(time.time() - started)} seconds.")
            await anyio.sleep(self._poll_interval_seconds)
            interaction = await self._aclient.interactions.get(id=interaction.id)
        return interaction

    @staticmethod
    def _raise_if_failed(interaction: Any) -> None:
        status = str(getattr(interaction, "status", ""))
        if status == COMPLETED_INTERACTION_STATUS:
            return
        errors = getattr(interaction, "errors", None) or []
        messages = [str(getattr(e, "message", e)) for e in errors]
        raise OmniVideoException(
            OmniErrorPayload(
                code="OMNI_INTERACTION_NOT_COMPLETED",
                message=f"Interaction ended with status '{status}': {'; '.join(messages) or 'no error details'}",
                retryable=False,
                details={"interaction_id": getattr(interaction, "id", None), "status": status},
            )
        )

    async def _download_output_video(self, output_video: Any) -> Tuple[bytes, str]:
        mime = self._normalize_mime(getattr(output_video, "mime_type", None)) or "video/mp4"
        data = getattr(output_video, "data", None)
        if data:
            return (base64.b64decode(data) if isinstance(data, str) else bytes(data)), mime

        uri = getattr(output_video, "uri", None)
        if not uri:
            raise OmniVideoException(
                OmniErrorPayload(
                    code="OMNI_OUTPUT_EMPTY",
                    message="Interaction completed without video data or uri",
                    retryable=False,
                )
            )
        await self._gemini.wait_for_file_active(name=self._file_name_from_uri(str(uri)))
        downloaded = await self._aclient.files.download(file=str(uri))
        return bytes(downloaded), mime

    @staticmethod
    def _file_name_from_uri(uri: str) -> str:
        match = FILE_ID_PATTERN.search(uri)
        if match is None:
            raise OmniVideoException(
                OmniErrorPayload(
                    code="OMNI_OUTPUT_URI_INVALID",
                    message=f"Could not extract file name from uri: {uri}",
                    retryable=False,
                    details={"uri": uri},
                )
            )
        return f"files/{match.group(1)}"

    async def _upload_to_s3(
        self,
        *,
        ctx: Context,
        user: UserORM,
        video: Tuple[bytes, str],
        prefix: str,
        notify: Notify,
    ) -> List[str]:
        from src.autobots.conn.aws.aws_s3 import get_public_s3
        from src.autobots.files.user_files import UserFiles

        data, mime = video
        ext = mimetypes.guess_extension(mime) or ".mp4"
        upload = UploadFile(filename=f"omni_{uuid.uuid4().hex[:8]}{ext}", file=BytesIO(data))
        user_files = UserFiles(user=user, s3=get_public_s3(object_prefix=prefix))

        self._notify(ctx, notify, "Video uploading to s3 starting.")
        metas = await user_files.upload_files(ctx=ctx, files=[upload])
        self._notify(ctx, notify, "Video uploading to s3 completed.")
        return [str(m.url) for m in metas if getattr(m, "url", None)]

    @staticmethod
    def _notify(ctx: Context, notify: Notify, message: str) -> None:
        if not notify:
            return
        try:
            notify(ctx, message)
        except Exception:
            logger.bind(ctx=ctx).warning("Failed to send omni notify: {}", message)

    async def generate_video(
        self,
        *,
        ctx: Context,
        user: UserORM,
        req: OmniVideoRequest,
        db: AsyncIOMotorDatabase = None,
        notify: Notify = None,
    ) -> OmniVideoResult:
        try:
            self._notify(ctx, notify, f"Video is generating using {req.model.value}.")
            started = time.time()

            async with httpx.AsyncClient(timeout=self._download_timeout) as http:
                contents = await self._build_input(req, http)

            interaction = await self._aclient.interactions.create(**self._build_create_kwargs(req, contents))
            interaction = await self._wait_for_completion(
                interaction,
                timeout_seconds=req.generation_config.request_timeout_seconds,
                ctx=ctx,
                notify=notify,
            )
            self._raise_if_failed(interaction)
            self._notify(ctx, notify, f"Video generation completed in {int(time.time() - started)} seconds.")

            output_video = getattr(interaction, "output_video", None)
            if output_video is None:
                raise OmniVideoException(
                    OmniErrorPayload(
                        code="OMNI_OUTPUT_EMPTY",
                        message="Interaction completed without a video output",
                        retryable=False,
                        details={"interaction_id": getattr(interaction, "id", None)},
                    )
                )

            self._notify(ctx, notify, "Video downloading started.")
            data, mime = await self._download_output_video(output_video)
            self._enforce_max_bytes(len(data), where="generated video download")
            self._notify(ctx, notify, "Video downloading completed.")

            provider_uri = getattr(output_video, "uri", None)
            generated = OmniGeneratedVideo(
                mime_type=mime,
                file_name=f"omni_1{mimetypes.guess_extension(mime) or '.mp4'}",
                provider_uri=str(provider_uri) if provider_uri else None,
            )

            s3_urls: List[str] = []
            if req.upload_to_s3:
                try:
                    s3_urls = await self._upload_to_s3(
                        ctx=ctx, user=user, video=(data, mime), prefix=req.s3_prefix, notify=notify
                    )
                except Exception as e:
                    if req.fail_on_s3_error:
                        raise
                    logger.bind(ctx=ctx).warning("S3 upload failed but fail_on_s3_error=False: {}", e)
            if s3_urls:
                generated.s3_url = s3_urls[0]

            return OmniVideoResult(
                model=req.model.value,
                interaction_id=getattr(interaction, "id", None) or None,
                videos=[generated],
                s3_urls=s3_urls,
                provider_uris=[generated.provider_uri] if generated.provider_uri else [],
            )

        except OmniVideoException:
            raise
        except Exception as e:
            logger.bind(ctx=ctx).exception("Omni generate_video error: {}", str(e))
            raise OmniVideoException(
                OmniErrorPayload(
                    code="OMNI_GENERATION_FAILED",
                    message=str(e),
                    retryable=is_retryable_exception(e),
                    details={"model": req.model.value},
                )
            ) from e


def get_gemini_omni_conn(ctx: Context, api_key: str) -> OmniVideoClient:
    return OmniVideoClient(api_key)
