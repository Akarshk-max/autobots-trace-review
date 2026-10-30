from __future__ import annotations

import base64
import binascii
import mimetypes
import os
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any, List, Optional, Tuple

import anyio
import httpx
from fastapi import UploadFile
from google import genai
from google.genai import types
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from src.autobots.conn.gemini.gen_image.gemini_image_models import (
    GeneratedImage,
    NanoBananaErrorPayload,
    NanoBananaException,
    NanoBananaGenerateConfig,
    NanoBananaImageInput,
    NanoBananaImageRequest,
    NanoBananaImageResult,
    _is_retryable_exception,
)
from src.autobots.data_model.context import Context
from src.autobots.data_model.app_state import AppState


def _default_timeout() -> httpx.Timeout:
    return httpx.Timeout(connect=10.0, read=60.0, write=20.0, pool=10.0)


class NanoBananaImageClient:
    """
    Async client for Nano Banana / Nano Banana Pro image generation & editing.
    """

    def __init__(
        self,
        api_key: str,
        *,
        vertexai: bool = False,
        project: Optional[str] = None,
        location: Optional[str] = None,
        http_options: Optional[types.HttpOptions] = None,
        download_timeout: Optional[httpx.Timeout] = None,
        download_max_bytes: int = 48 * 1024 * 1024,  # 25MB
        download_retries: int = 2,
        download_user_agent: str = "NanoBananaImageClient/1.0",
        allow_non_image_mime: bool = False,
        follow_redirects: bool = True,
    ):
        # if we are using vertex ai
        if vertexai:
            self._client = genai.Client(
                vertexai=True,
                project=project,
                location=location,
                http_options=http_options,
            )
        # otherwise this is simple fallback
        else:
            self._client = genai.Client(
                api_key=api_key,
                http_options=http_options,
            )

        self._aclient = self._client.aio

        self._download_timeout = download_timeout or _default_timeout()
        self._download_max_bytes = int(download_max_bytes)
        self._download_retries = int(download_retries)
        self._download_headers = {"user-agent": download_user_agent}
        self._allow_non_image_mime = bool(allow_non_image_mime)
        self._follow_redirects = bool(follow_redirects)

    async def aclose(self) -> None:
        try:
            await self._aclient.aclose()
        except Exception:
            pass

    def _infer_name_from_url(self, url: str) -> str:
        # i did not use urlparse here
        tail = url.split("?")[0].rstrip("/").split("/")[-1].strip()
        return tail or "image"

    def _enforce_max_bytes(self, n: int, *, where: str) -> None:
        if n > self._download_max_bytes:
            raise NanoBananaException(
                NanoBananaErrorPayload(
                    code="IMAGE_TOO_LARGE",
                    message=f"{where} exceeds max bytes limit ({n} > {self._download_max_bytes})",
                    retryable=False,
                    details={"max_bytes": self._download_max_bytes, "got_bytes": n},
                )
            )

    async def _read_file_bytes(self, path: str) -> bytes:
        p = Path(path)
        if not p.exists():
            raise NanoBananaException(
                NanoBananaErrorPayload(
                    code="IMAGE_INPUT_FILE_NOT_FOUND",
                    message=f"file_path not found: {path}",
                    retryable=False,
                    details={"file_path": path},
                )
            )

        # Non-blocking (reads in a thread as simple as it is i learn from ingestion code base)
        data: bytes = await anyio.to_thread.run_sync(p.read_bytes)
        self._enforce_max_bytes(len(data), where="file_path")
        return data

    # this method is responsible for downloading given image urls in config or input
    # properly and handling exception for timeout and max sizes
    async def _download_url_bytes(
        self, url: str, http: httpx.AsyncClient
    ) -> Tuple[bytes, httpx.Response]:
        last_exc: Optional[Exception] = None

        for attempt in range(self._download_retries + 1):
            try:
                async with http.stream(
                    "GET",
                    url,
                    headers=self._download_headers,
                    follow_redirects=self._follow_redirects,
                ) as r:
                    r.raise_for_status()

                    cl = r.headers.get("content-length")
                    if cl:
                        try:
                            self._enforce_max_bytes(int(cl), where="url content-length")
                        except ValueError:
                            pass

                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        if not chunk:
                            continue
                        buf.extend(chunk)
                        if len(buf) > self._download_max_bytes:
                            raise NanoBananaException(
                                NanoBananaErrorPayload(
                                    code="IMAGE_TOO_LARGE",
                                    message=f"url download exceeds max bytes limit ({len(buf)} > {self._download_max_bytes})",
                                    retryable=False,
                                    http_status=r.status_code,
                                    details={"max_bytes": self._download_max_bytes, "url": url},
                                )
                            )

                    return bytes(buf), r

            except NanoBananaException:
                raise
            except Exception as e:
                last_exc = e
                if attempt < self._download_retries:
                    continue
                raise NanoBananaException(
                    NanoBananaErrorPayload(
                        code="IMAGE_URL_DOWNLOAD_FAILED",
                        message=str(e),
                        retryable=_is_retryable_exception(e),
                        details={"url": url, "attempts": self._download_retries + 1},
                    )
                ) from e

        # unreachable section
        raise NanoBananaException(
            NanoBananaErrorPayload(
                code="IMAGE_URL_DOWNLOAD_FAILED",
                message=str(last_exc or "unknown error"),
                retryable=True,
                details={"url": url},
            )
        )

    def _normalize_mime(self, mime: Optional[str]) -> Optional[str]:
        if not mime:
            return None
        return mime.split(";")[0].strip().lower()

    def _validate_mime_is_image(self, mime: str, *, where: str) -> None:
        if self._allow_non_image_mime:
            return
        if mime and mime.startswith("image/"):
            return
        raise NanoBananaException(
            NanoBananaErrorPayload(
                code="IMAGE_MIME_NOT_IMAGE",
                message=f"{where} mime_type is not an image: {mime}",
                retryable=False,
                details={"mime_type": mime},
            )
        )

    async def _fetch_image_bytes(
        self, img: NanoBananaImageInput, http: httpx.AsyncClient
    ) -> Tuple[bytes, str, str]:
        """
        Returns: (bytes, mime_type, file_name)
        """
        if img.file_path:
            data = await self._read_file_bytes(img.file_path)
            file_name = img.file_name or os.path.basename(img.file_path)
            mime_type = img.mime_type or (
                mimetypes.guess_type(file_name)[0] or "application/octet-stream"
            )
            mime_type = self._normalize_mime(mime_type) or "application/octet-stream"
            if mime_type != "application/octet-stream":
                self._validate_mime_is_image(mime_type, where="file_path")
            return data, mime_type, file_name

        if img.url:
            url = str(img.url)
            data, r = await self._download_url_bytes(url, http)
            file_name = img.file_name or self._infer_name_from_url(url)

            mime_type = img.mime_type
            if not mime_type:
                mime_type = r.headers.get("content-type")
            if not mime_type:
                mime_type = mimetypes.guess_type(file_name)[0]
            mime_type = self._normalize_mime(mime_type) or "application/octet-stream"

            # Validate it's an image unless explicitly allowed
            if mime_type != "application/octet-stream":
                self._validate_mime_is_image(mime_type, where="url")

            return data, mime_type, file_name

        if img.base64_content:
            raw = img.base64_content.strip()

            mime_type: Optional[str] = img.mime_type
            b64 = raw
            if raw.startswith("data:") and "base64," in raw:
                header, b64 = raw.split("base64,", 1)
                header = header.replace("data:", "").replace(";", "").strip()
                mime_type = mime_type or header or None

            try:
                data = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError) as e:
                raise NanoBananaException(
                    NanoBananaErrorPayload(
                        code="IMAGE_BASE64_INVALID",
                        message=f"Invalid base64_content: {e}",
                        retryable=False,
                    )
                ) from e

            self._enforce_max_bytes(len(data), where="base64_content")

            file_name = img.file_name or "image"
            mime_type = self._normalize_mime(mime_type) or "application/octet-stream"

            if mime_type != "application/octet-stream":
                self._validate_mime_is_image(mime_type, where="base64_content")

            if mime_type and "." not in file_name:
                ext = mimetypes.guess_extension(mime_type) or ""
                file_name = f"{file_name}{ext}"

            return data, mime_type, file_name

        raise NanoBananaException(
            NanoBananaErrorPayload(
                code="IMAGE_INPUT_INVALID",
                message="Invalid image input (no source).",
                retryable=False,
            )
        )

    def _to_sdk_config(self, cfg: NanoBananaGenerateConfig) -> types.GenerateContentConfig:
        safety_settings = None
        if cfg.safety_settings:
            safety_settings = [
                types.SafetySetting(category=s.category.value, threshold=s.threshold.value)
                for s in cfg.safety_settings
            ]

        image_config = None
        if cfg.image_config:
            image_config = types.ImageConfig(
                aspect_ratio=cfg.image_config.aspect_ratio.value
                if cfg.image_config.aspect_ratio
                else None,
                image_size=cfg.image_config.image_size.value
                if cfg.image_config.image_size
                else None,
            )

        return types.GenerateContentConfig(
            response_modalities=[m.value for m in cfg.response_modalities],
            image_config=image_config,
            safety_settings=safety_settings,
            temperature=cfg.temperature,
            top_p=cfg.top_p,
            top_k=cfg.top_k,
            candidate_count=cfg.candidate_count,
            seed=cfg.seed,
            max_output_tokens=cfg.max_output_tokens,
            stop_sequences=cfg.stop_sequences,
            presence_penalty=cfg.presence_penalty,
            frequency_penalty=cfg.frequency_penalty,
            tools=cfg.tools,
        )

    # this method is directly taken from documentation with little bit changes
    def _parse_response(
        self, resp: types.GenerateContentResponse
    ) -> Tuple[List[str], List[Tuple[bytes, str]]]:
        """
        Returns: (texts, images[(bytes, mime_type)])
        """
        parts = getattr(resp, "parts", None)

        if not parts and getattr(resp, "candidates", None):
            try:
                parts = resp.candidates[0].content.parts
            except Exception:
                parts = []

        texts: List[str] = []
        images: List[Tuple[bytes, str]] = []

        for p in parts or []:
            t = getattr(p, "text", None)
            if t:
                texts.append(t)

            blob = getattr(p, "inline_data", None)
            if blob is None:
                continue

            mime_type = getattr(blob, "mime_type", None) or "application/octet-stream"
            mime_type = self._normalize_mime(mime_type) or "application/octet-stream"
            data = getattr(blob, "data", None)

            if isinstance(data, str):
                try:
                    data_bytes = base64.b64decode(data)
                except Exception:
                    data_bytes = b""
            else:
                data_bytes = data or b""

            if data_bytes:
                images.append((data_bytes, mime_type))

        return texts, images

    # this method is responsible for uploading file to s3 bucket and get a s3 url
    async def _maybe_upload_to_s3(
        self,
        *,
        ctx: Any,
        user: Any,
        images: List[Tuple[bytes, str]],
        prefix: str,
        db: Any = None,
        notify: Any = None,
    ) -> List[str]:
        if not images:
            return []

        try:
            from src.autobots.conn.aws.aws_s3 import get_public_s3
            from src.autobots.files.user_files import UserFiles
        except Exception as e:
            raise NanoBananaException(
                NanoBananaErrorPayload(
                    code="S3_UPLOAD_IMPORT_ERROR",
                    message=f"upload_to_s3=True but S3 modules not importable: {e}",
                    retryable=False,
                )
            )

        aws_s3 = get_public_s3(object_prefix=prefix)
        user_files = UserFiles(user=user, s3=aws_s3)

        uploads: List[UploadFile] = []
        for i, (b, mime) in enumerate(images, start=1):
            ext = mimetypes.guess_extension(mime) or ".png"
            filename = f"nanobanana_{i}_{uuid.uuid4().hex[:8]}{ext}"
            uploads.append(UploadFile(filename=filename, file=BytesIO(b)))

        if notify:
            try:
                await notify(
                    ctx=ctx, event="nanobanana:s3_upload_start", data={"count": len(uploads)}
                )
            except Exception:
                logger.bind(ctx=ctx).warning("Failed to send s3_upload_start notify")

        metas = await user_files.upload_files(ctx=ctx, files=uploads)
        urls = [str(m.url) for m in metas if getattr(m, "url", None)]

        if notify:
            try:
                await notify(ctx=ctx, event="nanobanana:s3_upload_done", data={"count": len(urls)})
            except Exception:
                logger.bind(ctx=ctx).warning("Failed to send s3_upload_done notify")

        return urls

    # this is the method for responsible for generating
    # image and also editing existing image.
    @retry(wait=wait_exponential_jitter(0.5, 3), stop=stop_after_attempt(4), reraise=True)
    async def generate_image(
        self,
        *,
        ctx: Any,
        app_state: AppState,
        user: Any,
        req: NanoBananaImageRequest,
        db: Any = None,
        notify: Any = None,
    ) -> NanoBananaImageResult:
        try:
            sdk_cfg = self._to_sdk_config(req.generation_config)

            if notify:
                try:
                    await notify(
                        ctx=ctx,
                        event="nanobanana:request_start",
                        data={
                            "model": req.model.value,
                            "input_images": len(req.images or []),
                            "modalities": [
                                m.value for m in req.generation_config.response_modalities
                            ],
                            "upload_to_s3": req.upload_to_s3,
                        },
                    )
                except Exception:
                    logger.bind(ctx=ctx).warning("Failed to send request_start notify")

            contents: List[Any] = [req.prompt]

            if req.images:
                async with httpx.AsyncClient(timeout=self._download_timeout) as http:
                    for img in req.images:
                        data, mime, _name = await self._fetch_image_bytes(img, http)
                        contents.append(
                            types.Part(inline_data=types.Blob(mime_type=mime, data=data))
                        )

            resp = await self._aclient.models.generate_content(
                model=req.model.value,
                contents=contents,
                config=sdk_cfg,
            )

            texts, images = self._parse_response(resp)

            out_images: List[GeneratedImage] = []
            for idx, (b, mime) in enumerate(images, start=1):
                ext = mimetypes.guess_extension(mime) or ".png"
                out_images.append(
                    GeneratedImage(
                        mime_type=mime,
                        bytes_b64=base64.b64encode(b).decode("utf-8"),
                        file_name=f"nanobanana_{idx}{ext}",
                    )
                )

            s3_urls: List[str] = []
            if req.upload_to_s3 and images:
                try:
                    s3_urls = await self._maybe_upload_to_s3(
                        ctx=ctx,
                        user=user,
                        images=images,
                        prefix=req.s3_prefix,
                        db=db,
                        notify=notify,
                    )
                except Exception as e:
                    if req.fail_on_s3_error:
                        raise
                    logger.bind(ctx=ctx).warning(
                        "S3 upload failed but fail_on_s3_error=False: {}", e
                    )

            if notify:
                try:
                    await notify(
                        ctx=ctx,
                        event="nanobanana:request_done",
                        data={
                            "texts": len(texts),
                            "images": len(out_images),
                            "s3_urls": len(s3_urls),
                        },
                    )
                except Exception:
                    logger.bind(ctx=ctx).warning("Failed to send request_done notify")

            return NanoBananaImageResult(
                model=req.model.value,
                texts=texts,
                images=out_images,
                s3_urls=s3_urls,
            )

        except NanoBananaException:
            raise
        except Exception as e:
            retryable = _is_retryable_exception(e)
            logger.bind(ctx=ctx).exception("NanoBanana generate_image error: {}", str(e))
            raise NanoBananaException(
                NanoBananaErrorPayload(
                    code="NANOBANANA_GENERATION_FAILED",
                    message=str(e),
                    retryable=retryable,
                    details={"model": req.model.value},
                )
            ) from e


def get_gemini_nano_conn(ctx: Context, api_key: str) -> NanoBananaImageClient:
    return NanoBananaImageClient(api_key)
