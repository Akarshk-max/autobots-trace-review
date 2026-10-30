import base64
import binascii
import mimetypes
import os
import time
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
from motor.motor_asyncio import AsyncIOMotorDatabase
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from src.autobots.conn.gemini.gen_video.gemini_video_models import (
    GeneratedVideo,
    VeoErrorPayload,
    VeoException,
    VeoMediaInput,
    VeoModel,
    VeoVideoRequest,
    VeoVideoResult,
    _is_retryable_exception,
    supports_enhance_prompt,
)
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


def _default_timeout() -> httpx.Timeout:
    return httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)


class VeoVideoClient:
    def __init__(
        self,
        api_key: str,
        *,
        vertexai: bool = False,
        project: Optional[str] = None,
        location: Optional[str] = None,
        http_options: Optional[types.HttpOptions] = None,
        download_timeout: Optional[httpx.Timeout] = None,
        download_max_bytes: int = 512 * 1024 * 1024,
        download_retries: int = 2,
        download_user_agent: str = "VeoVideoClient/1.0",
        follow_redirects: bool = True,
        allow_non_video_mime: bool = False,
        allow_non_image_mime: bool = False,
    ):
        self._vertexai = bool(vertexai)
        if vertexai:
            self._client = genai.Client(
                vertexai=True,
                project=project,
                location=location,
                http_options=http_options,
            )
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
        self._follow_redirects = bool(follow_redirects)
        self._allow_non_video_mime = bool(allow_non_video_mime)
        self._allow_non_image_mime = bool(allow_non_image_mime)

    async def aclose(self) -> None:
        try:
            await self._aclient.aclose()
        except Exception:
            pass

    def _infer_name_from_url(self, url: str) -> str:
        tail = url.split("?")[0].rstrip("/").split("/")[-1].strip()
        return tail or "media"

    def _normalize_mime(self, mime: Optional[str]) -> Optional[str]:
        if not mime:
            return None
        return mime.split(";")[0].strip().lower()

    def _enforce_max_bytes(self, n: int, *, where: str) -> None:
        if n > self._download_max_bytes:
            raise VeoException(
                VeoErrorPayload(
                    code="MEDIA_TOO_LARGE",
                    message=f"{where} exceeds max bytes limit ({n} > {self._download_max_bytes})",
                    retryable=False,
                    details={"max_bytes": self._download_max_bytes, "got_bytes": n},
                )
            )

    async def _read_file_bytes(self, path: str) -> bytes:
        p = Path(path)
        if not p.exists():
            raise VeoException(
                VeoErrorPayload(
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
                            raise VeoException(
                                VeoErrorPayload(
                                    code="MEDIA_TOO_LARGE",
                                    message=f"url download exceeds max bytes limit ({len(buf)} > {self._download_max_bytes})",
                                    retryable=False,
                                    http_status=r.status_code,
                                    details={"max_bytes": self._download_max_bytes, "url": url},
                                )
                            )
                    return bytes(buf), r
            except VeoException:
                raise
            except Exception as e:
                last_exc = e
                if attempt < self._download_retries:
                    continue
                raise VeoException(
                    VeoErrorPayload(
                        code="MEDIA_URL_DOWNLOAD_FAILED",
                        message=str(e),
                        retryable=_is_retryable_exception(e),
                        details={"url": url, "attempts": self._download_retries + 1},
                    )
                ) from e
        raise VeoException(
            VeoErrorPayload(
                code="MEDIA_URL_DOWNLOAD_FAILED",
                message=str(last_exc or "unknown error"),
                retryable=True,
                details={"url": url},
            )
        )

    def _validate_image_mime(self, mime: str, *, where: str) -> None:
        if self._allow_non_image_mime:
            return
        if mime and mime.startswith("image/"):
            return
        raise VeoException(
            VeoErrorPayload(
                code="IMAGE_MIME_NOT_IMAGE",
                message=f"{where} mime_type is not an image: {mime}",
                retryable=False,
                details={"mime_type": mime},
            )
        )

    def _validate_video_mime(self, mime: str, *, where: str) -> None:
        if self._allow_non_video_mime:
            return
        if mime and mime.startswith("video/"):
            return
        raise VeoException(
            VeoErrorPayload(
                code="VIDEO_MIME_NOT_VIDEO",
                message=f"{where} mime_type is not a video: {mime}",
                retryable=False,
                details={"mime_type": mime},
            )
        )

    async def _fetch_bytes_input(self, media: VeoMediaInput, http: httpx.AsyncClient) -> Tuple[bytes, str, str]:
        if media.file_path:
            data = await self._read_file_bytes(media.file_path)
            file_name = media.file_name or os.path.basename(media.file_path)
            mime_type = media.mime_type or (mimetypes.guess_type(file_name)[0] or "application/octet-stream")
            mime_type = self._normalize_mime(mime_type) or "application/octet-stream"
            return data, mime_type, file_name

        if media.url:
            url = str(media.url)
            data, r = await self._download_url_bytes(url, http)
            file_name = media.file_name or self._infer_name_from_url(url)
            mime_type = media.mime_type or r.headers.get("content-type") or mimetypes.guess_type(file_name)[0]
            mime_type = self._normalize_mime(mime_type) or "application/octet-stream"
            return data, mime_type, file_name

        if media.base64_content:
            raw = media.base64_content.strip()
            mime_type: Optional[str] = media.mime_type
            b64 = raw
            if raw.startswith("data:") and "base64," in raw:
                header, b64 = raw.split("base64,", 1)
                header = header.replace("data:", "").replace(";", "").strip()
                mime_type = mime_type or header or None
            try:
                data = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError) as e:
                raise VeoException(
                    VeoErrorPayload(
                        code="MEDIA_BASE64_INVALID",
                        message=f"Invalid base64_content: {e}",
                        retryable=False,
                    )
                ) from e
            self._enforce_max_bytes(len(data), where="base64_content")
            file_name = media.file_name or "media"
            mime_type = self._normalize_mime(mime_type) or "application/octet-stream"
            if mime_type and "." not in file_name:
                ext = mimetypes.guess_extension(mime_type) or ""
                file_name = f"{file_name}{ext}"
            return data, mime_type, file_name

        raise VeoException(
            VeoErrorPayload(
                code="MEDIA_INPUT_INVALID",
                message="Invalid media input (no bytes source).",
                retryable=False,
            )
        )

    async def _to_sdk_image(self, img: VeoMediaInput, http: httpx.AsyncClient, *, where: str) -> types.Image:
        data, mime, _name = await self._fetch_bytes_input(img, http)
        mime = self._normalize_mime(mime) or "application/octet-stream"
        if mime != "application/octet-stream":
            self._validate_image_mime(mime, where=where)
        return types.Image(image_bytes=data, mime_type=mime if mime != "application/octet-stream" else None)

    async def _upload_video_to_gemini(self, data: bytes, mime: str, name: str) -> types.File:
        mime = self._normalize_mime(mime) or "application/octet-stream"
        if mime != "application/octet-stream":
            self._validate_video_mime(mime, where="video upload")
        cfg = types.UploadFileConfig(mime_type=mime if mime != "application/octet-stream" else None, display_name=name)
        buf = BytesIO(data)
        buf.seek(0)
        try:
            return await self._aclient.files.upload(file=buf, config=cfg)
        except Exception:
            return await anyio.to_thread.run_sync(self._client.files.upload, file=buf, config=cfg)

    async def _to_sdk_video(self, vid: VeoMediaInput, http: httpx.AsyncClient, *, where: str) -> types.Video:
        if vid.gemini_uri:
            mime = self._normalize_mime(vid.mime_type) or "video/mp4"
            if mime != "application/octet-stream":
                self._validate_video_mime(mime, where=where)
            return types.Video(uri=str(vid.gemini_uri), mime_type=mime)

        data, mime, name = await self._fetch_bytes_input(vid, http)
        mime = self._normalize_mime(mime) or "application/octet-stream"
        if mime != "application/octet-stream":
            self._validate_video_mime(mime, where=where)
        f = await self._upload_video_to_gemini(data=data, mime=mime, name=name)
        out_mime = self._normalize_mime(mime) or self._normalize_mime(getattr(f, "mime_type", None)) or "video/mp4"
        return types.Video(uri=getattr(f, "uri", None) or getattr(f, "name", None), mime_type=out_mime)

    # def _to_sdk_config(self, cfg) -> types.GenerateVideosConfig:
    #     last_frame = None
    #     mask = None
    #     reference_images = None

    #     return types.GenerateVideosConfig(
    #         negative_prompt=cfg.negative_prompt,
    #         enhance_prompt=cfg.enhance_prompt,
    #         number_of_videos=cfg.number_of_videos,
    #         duration_seconds=cfg.duration_seconds,
    #         fps=cfg.fps,
    #         aspect_ratio=cfg.aspect_ratio.value if cfg.aspect_ratio else None,
    #         resolution=cfg.resolution.value if cfg.resolution else None,
    #         compression_quality=cfg.compression_quality.value if cfg.compression_quality else None,
    #         person_generation=cfg.person_generation.value if cfg.person_generation else None,
    #         generate_audio=(cfg.generate_audio if self._vertexai else None),
    #         last_frame=last_frame,
    #         mask=mask,
    #         reference_images=reference_images,
    #         output_gcs_uri=cfg.output_gcs_uri,
    #         pubsub_topic=cfg.pubsub_topic,
    #     )
    

    def _to_sdk_config(self, cfg, *, req_model: VeoModel) -> types.GenerateVideosConfig:
        kwargs = {
            "negative_prompt": cfg.negative_prompt,
            "number_of_videos": cfg.number_of_videos,
            "duration_seconds": cfg.duration_seconds,
            # "fps": cfg.fps,
            "aspect_ratio": cfg.aspect_ratio.value if cfg.aspect_ratio else None,
            "resolution": cfg.resolution.value if cfg.resolution else None,
            "compression_quality": cfg.compression_quality.value if cfg.compression_quality else None,
            "person_generation": cfg.person_generation.value if cfg.person_generation else None,
            "generate_audio": (cfg.generate_audio if self._vertexai else None),
            "output_gcs_uri": cfg.output_gcs_uri,
            "pubsub_topic": cfg.pubsub_topic,
            # last_frame/mask/reference_images are attached later in _finalize_sdk_config
        }

        if supports_enhance_prompt(req_model) and bool(cfg.enhance_prompt):
            kwargs["enhance_prompt"] = True
        
        if req_model is VeoModel.VEO_2_0_GENERATE_001:
            kwargs.pop("person_generation", None)

        kwargs = {k: v for k, v in kwargs.items() if v is not None}

        return types.GenerateVideosConfig(**kwargs)


    async def _maybe_upload_to_s3(
        self,
        *,
        ctx: Any,
        user: Any,
        videos: List[Tuple[bytes, str]],
        prefix: str,
        db: Any = None,
        notify: Any = None,
    ) -> List[str]:
        if not videos:
            return []
        try:
            from src.autobots.conn.aws.aws_s3 import get_public_s3
            from src.autobots.files.user_files import UserFiles
        except Exception as e:
            raise VeoException(
                VeoErrorPayload(
                    code="S3_UPLOAD_IMPORT_ERROR",
                    message=f"upload_to_s3=True but S3 modules not importable: {e}",
                    retryable=False,
                )
            )

        aws_s3 = get_public_s3(object_prefix=prefix)
        user_files = UserFiles(user=user, s3=aws_s3)

        uploads: List[UploadFile] = []
        for i, (b, mime) in enumerate(videos, start=1):
            ext = mimetypes.guess_extension(mime) or ".mp4"
            filename = f"veo_{i}_{uuid.uuid4().hex[:8]}{ext}"
            uploads.append(UploadFile(filename=filename, file=BytesIO(b)))

        if notify:
            try:
                notify(ctx , "Video uploading to s3 starting.")
            except Exception:
                logger.bind(ctx=ctx).warning("Failed to send veo:s3_upload_start notify")

        metas = await user_files.upload_files(ctx=ctx, files=uploads)
        urls = [str(m.url) for m in metas if getattr(m, "url", None)]

        if notify:
            try:
                notify(ctx , "Video uploading to s3 completed.")
            except Exception:
                logger.bind(ctx=ctx).warning("Failed to send veo:s3_upload_done notify")

        return urls

    async def _build_sdk_extras(self, cfg, http: httpx.AsyncClient) -> Tuple[Optional[types.Image], Optional[types.Video], Optional[types.Image], Optional[types.VideoGenerationMask], Optional[List[types.VideoGenerationReferenceImage]]]:
        source_image = None
        source_video = None
        last_frame = None
        mask = None
        reference_images = None

        if cfg.last_frame:
            last_frame = await self._to_sdk_image(cfg.last_frame, http, where="last_frame")

        if cfg.mask:
            mask_img = await self._to_sdk_image(cfg.mask.mask_image, http, where="mask_image")
            mask = types.VideoGenerationMask(mask_mode=cfg.mask.mask_mode.value, mask_image=mask_img)

        if cfg.reference_images:
            refs: List[types.VideoGenerationReferenceImage] = []
            for r in cfg.reference_images:
                img = await self._to_sdk_image(r.image, http, where="reference_image")
                refs.append(
                    types.VideoGenerationReferenceImage(
                        reference_id=r.reference_id,
                        reference_image=img,
                        reference_type=r.reference_type.value,
                    )
                )
            reference_images = refs

        return source_image, source_video, last_frame, mask, reference_images

    async def _finalize_sdk_config(self, req: VeoVideoRequest, http: httpx.AsyncClient) -> types.GenerateVideosConfig:
        cfg = self._to_sdk_config(req.generation_config, req_model=req.model)
        _si, _sv, last_frame, mask, reference_images = await self._build_sdk_extras(req.generation_config, http)
        cfg.last_frame = last_frame
        cfg.mask = mask
        cfg.reference_images = reference_images
        return cfg


    def _extract_generated_videos(self, op: Any) -> List[Any]:
        resp = getattr(op, "response", None) or getattr(op, "result", None) or getattr(op, "operation", None)
        if resp is None:
            return []
        out = getattr(resp, "generated_videos", None)
        if out is None:
            out = getattr(resp, "generatedVideos", None)
        return list(out or [])

    async def _download_video_bytes(self, video: types.Video) -> bytes:
        try:
            data = await self._aclient.files.download(file=video)
            if isinstance(data, (bytes, bytearray)):
                return bytes(data)
            vb = getattr(video, "video_bytes", None)
            return bytes(vb or b"")
        except Exception:
            data = await anyio.to_thread.run_sync(self._client.files.download, file=video)
            if isinstance(data, (bytes, bytearray)):
                return bytes(data)
            vb = getattr(video, "video_bytes", None)
            return bytes(vb or b"")

    @retry(wait=wait_exponential_jitter(0.8, 4), stop=stop_after_attempt(4), reraise=True)
    async def generate_video(
        self,
        *,
        ctx: Context,
        user: UserORM,
        req: VeoVideoRequest,
        db: AsyncIOMotorDatabase = None,
        notify: Notify = None,
    ) -> VeoVideoResult:
        try:
            if notify:
                try:
                    notify(
                        ctx,
                        f"Video is generating using {req.model.value}."
                    )
                except Exception:
                    logger.bind(ctx=ctx).warning("Failed to send veo:request_start notify")

            async with httpx.AsyncClient(timeout=self._download_timeout) as http:
                sdk_cfg = await self._finalize_sdk_config(req, http)

                source_image = None
                source_video = None

                if req.image:
                    source_image = await self._to_sdk_image(req.image, http, where="image")
                if req.video:
                    source_video = await self._to_sdk_video(req.video, http, where="video")

                source = types.GenerateVideosSource(prompt=req.prompt, image=source_image, video=source_video)

                op = await self._aclient.models.generate_videos(
                    model=req.model.value,
                    source=source,
                    config=sdk_cfg,
                )

                t0 = time.time()
                interval = float(req.generation_config.poll_interval_seconds)
                timeout = float(req.generation_config.poll_timeout_seconds)

                while not bool(getattr(op, "done", False)):
                    if (time.time() - t0) > timeout:
                        raise VeoException(
                            VeoErrorPayload(
                                code="VEO_POLL_TIMEOUT",
                                message=f"Timed out waiting for video generation (>{timeout}s)",
                                retryable=True,
                                details={"model": req.model.value, "timeout_seconds": timeout},
                            )
                        )
                    if notify:
                        try:
                            notify(
                                ctx,
                                f"Video generation time elapsed is {int(time.time() - t0)} seconds."
                            )
                        except Exception:
                            pass
                    await anyio.sleep(interval)
                    op = await self._aclient.operations.get(op)

                if notify:
                    try:
                        notify(
                                ctx,
                                f"Video generation completed in {int(time.time() - t0)} seconds."
                            )
                    except Exception:
                        pass

                generated = self._extract_generated_videos(op)
                provider_uris: List[str] = []
                downloaded: List[Tuple[bytes, str]] = []
                videos_out: List[GeneratedVideo] = []

                if notify:
                    try:
                        notify(ctx , "Video downloading started")
                    except Exception:
                        pass

                for idx, gv in enumerate(generated, start=1):
                    v = getattr(gv, "video", None)
                    if v is None:
                        continue
                    uri = getattr(v, "uri", None)
                    if uri:
                        provider_uris.append(str(uri))
                    mime = self._normalize_mime(getattr(v, "mime_type", None)) or "video/mp4"
                    data = await self._download_video_bytes(v)
                    if not data:
                        continue
                    self._enforce_max_bytes(len(data), where="generated video download")
                    downloaded.append((data, mime))
                    ext = mimetypes.guess_extension(mime) or ".mp4"
                    videos_out.append(
                        GeneratedVideo(
                            mime_type=mime,
                            file_name=f"veo_{idx}{ext}",
                            provider_uri=str(uri) if uri else None,
                        )
                    )

                if notify:
                    try:
                        notify(ctx , "Video Downloading Completed.")
                    except Exception:
                        pass

                s3_urls: List[str] = []
                if req.upload_to_s3 and downloaded:
                    try:
                        s3_urls = await self._maybe_upload_to_s3(
                            ctx=ctx,
                            user=user,
                            videos=downloaded,
                            prefix=req.s3_prefix,
                            db=db,
                            notify=notify,
                        )
                    except Exception as e:
                        if req.fail_on_s3_error:
                            raise
                        logger.bind(ctx=ctx).warning("S3 upload failed but fail_on_s3_error=False: {}", e)

                for i in range(min(len(videos_out), len(s3_urls))):
                    videos_out[i].s3_url = s3_urls[i]

                return VeoVideoResult(
                    model=req.model.value,
                    texts=[],
                    videos=videos_out,
                    s3_urls=s3_urls,
                    provider_uris=provider_uris,
                )

        except VeoException:
            raise
        except Exception as e:
            retryable = _is_retryable_exception(e)
            logger.bind(ctx=ctx).exception("Veo generate_video error: {}", str(e))
            raise VeoException(
                VeoErrorPayload(
                    code="VEO_GENERATION_FAILED",
                    message=str(e),
                    retryable=retryable,
                    details={"model": req.model.value},
                )
            ) from e


def get_gemini_veo_conn(ctx: Context, api_key: str) -> VeoVideoClient:
    return VeoVideoClient(api_key)