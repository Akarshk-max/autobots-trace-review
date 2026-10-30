import asyncio
import os
import random
import string
import tempfile
from datetime import timedelta
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx
import xai_sdk
from fastapi import UploadFile
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.grok.grok_video.grok_video_models import (
    GeneratedVideo,
    GrokVideoModel,
    VideoAspectRatioType,
    VideoGenerationRequest,
    VideoGenerationResponse,
    VideoResolutionType,
    XAIVideoError,
    XAIVideoFetchError,
    XAIVideoGenerateError,
    XAIVideoSaveError,
)
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.files.user_files_model import FileMeta
from src.autobots.user.user_orm_model import UserORM

ProgressCallback = Callable[[int, int], None]


class XAIVideoManager:
    async def generate(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        *,
        prompt: str,
        model: GrokVideoModel = GrokVideoModel.grok_imagine_video,
        duration: Optional[int] = None,
        aspect_ratio: VideoAspectRatioType | None = None,
        resolution: VideoResolutionType | None = None,
        image_url: Optional[str] = None,
        video_url: Optional[str] = None,
        fetch_timeout_sec: float = 300.0,
        on_fetch_progress: ProgressCallback | None = None,
        max_fetch_bytes: Optional[int] = None,
        s3_object_prefix: str = "grok-video",
        filename_prefix: str = "video",
        ext: str = ".mp4",
        poll_timeout_sec: Optional[float] = None,
        poll_interval_sec: Optional[float] = None,
    ) -> VideoGenerationResponse:
        req = VideoGenerationRequest(
            prompt=prompt,
            model=model,
            duration=duration,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            image_url=image_url,
            video_url=video_url,
            poll_timeout_sec=poll_timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )

        warnings: list[str] = []

        try:
            if image_url and video_url:
                raise XAIVideoGenerateError("Only one of image_url or video_url may be provided.")

            call_duration = duration
            call_aspect_ratio = aspect_ratio
            call_resolution = resolution

            # xAI docs: editing existing videos does not support duration/aspect_ratio/resolution.
            if video_url:
                if duration is not None:
                    warnings.append(
                        "Ignoring duration because xAI video editing retains the input video's duration."
                    )
                    call_duration = None
                if aspect_ratio is not None:
                    warnings.append(
                        "Ignoring aspect_ratio because xAI video editing retains the input video's aspect ratio."
                    )
                    call_aspect_ratio = None
                if resolution is not None:
                    warnings.append(
                        "Ignoring resolution because xAI video editing retains the input video's resolution (capped at 720p)."
                    )
                    call_resolution = None

            resp = await self._call_xai(
                client,
                prompt=prompt,
                model=model.value,
                duration=call_duration,
                aspect_ratio=call_aspect_ratio,
                resolution=call_resolution,
                image_url=image_url,
                video_url=video_url,
                poll_timeout_sec=poll_timeout_sec,
                poll_interval_sec=poll_interval_sec,
            )

            revised_prompt = getattr(resp, "prompt", None)
            provider_url = self._extract_url(resp)
            duration_sec = self._extract_duration(resp)
            respect_moderation = self._extract_respect_moderation(resp)
            resolved_model = self._extract_model(resp) or model.value

            if not provider_url:
                raise XAIVideoGenerateError("SDK response missing generated video URL.")

            normalized_ext = ext if ext.startswith(".") else f".{ext}"

            tmp_path, content_type, size_bytes = await self._fetch_https_to_tempfile(
                ctx=ctx,
                url=provider_url,
                timeout_sec=fetch_timeout_sec,
                on_progress=on_fetch_progress,
                max_bytes=max_fetch_bytes,
                suffix=normalized_ext,
            )

            try:
                s3_url = await self._upload_tempfile_to_s3(
                    ctx=ctx,
                    user=user,
                    tmp_path=tmp_path,
                    s3_object_prefix=s3_object_prefix,
                    filename_prefix=filename_prefix,
                    ext=normalized_ext,
                )
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    logger.bind(ctx=ctx, user=user).info(f"[GROK-VIDEO] os.unlink({tmp_path}) failed due to OSError.")
                    pass

            videos = [
                GeneratedVideo(
                    index=0,
                    revised_prompt=revised_prompt,
                    url=s3_url,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    duration_sec=duration_sec,
                    respect_moderation=respect_moderation,
                    model=resolved_model,
                )
            ]

            logger.bind(ctx=ctx, user=user).info(f"[GROK-VIDEO] Generated n={len(videos)} model={model} -> uploaded_to_s3")
            return VideoGenerationResponse(request=req, videos=videos, warnings=warnings)

        except XAIVideoError:
            raise
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(f"[GROK-VIDEO] Failed generate error={exc}")
            raise XAIVideoGenerateError("Failed to generate video(s)") from exc

    async def generate_many_prompts(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        *,
        prompts: list[str],
        concurrency: int = 3,
        model: GrokVideoModel = GrokVideoModel.grok_imagine_video,
        duration: Optional[int] = None,
        aspect_ratio: VideoAspectRatioType | None = None,
        resolution: VideoResolutionType | None = None,
        image_url: Optional[str] = None,
        video_url: Optional[str] = None,
        fetch_timeout_sec: float = 300.0,
        max_fetch_bytes: Optional[int] = None,
        on_prompt_complete: Callable[[int, str, object], None] | None = None,
        s3_object_prefix: str = "grok-video",
        filename_prefix: str = "video",
        ext: str = ".mp4",
        poll_timeout_sec: Optional[float] = None,
        poll_interval_sec: Optional[float] = None,
    ) -> dict[str, VideoGenerationResponse]:
        """
        Batch over many prompts with bounded concurrency.
        Returns only successful prompt->response entries.
        """
        sem = asyncio.Semaphore(max(1, concurrency))
        results: dict[str, object] = {}

        async def _one(idx: int, p: str) -> None:
            async with sem:
                try:
                    out = await self.generate(
                        ctx=ctx,
                        user=user,
                        db=db,
                        client=client,
                        prompt=p,
                        model=model,
                        duration=duration,
                        aspect_ratio=aspect_ratio,
                        resolution=resolution,
                        image_url=image_url,
                        video_url=video_url,
                        fetch_timeout_sec=fetch_timeout_sec,
                        max_fetch_bytes=max_fetch_bytes,
                        s3_object_prefix=s3_object_prefix,
                        filename_prefix=filename_prefix,
                        ext=ext,
                        poll_timeout_sec=poll_timeout_sec,
                        poll_interval_sec=poll_interval_sec,
                    )
                    results[p] = out
                    if on_prompt_complete:
                        on_prompt_complete(idx, p, out)
                except Exception as exc:
                    results[p] = exc
                    if on_prompt_complete:
                        on_prompt_complete(idx, p, exc)

        await asyncio.gather(*(asyncio.create_task(_one(i, p)) for i, p in enumerate(prompts)))

        out: dict[str, VideoGenerationResponse] = {}
        for p, r in results.items():
            if isinstance(r, BaseException):
                continue
            out[p] = r
        return out

    async def _call_xai(
        self,
        client: xai_sdk.AsyncClient,
        *,
        prompt: str,
        model: str,
        duration: Optional[int],
        aspect_ratio: VideoAspectRatioType | None,
        resolution: VideoResolutionType | None,
        image_url: Optional[str],
        video_url: Optional[str],
        poll_timeout_sec: Optional[float],
        poll_interval_sec: Optional[float],
    ) -> Any:
        kwargs: dict[str, Any] = {
            "prompt": prompt,
            "model": model,
        }

        if duration is not None:
            kwargs["duration"] = duration
        if aspect_ratio is not None:
            kwargs["aspect_ratio"] = aspect_ratio
        if resolution is not None:
            kwargs["resolution"] = resolution
        if image_url:
            kwargs["image_url"] = image_url
        if video_url:
            kwargs["video_url"] = video_url
        if poll_timeout_sec is not None:
            kwargs["timeout"] = timedelta(seconds=poll_timeout_sec)
        if poll_interval_sec is not None:
            kwargs["interval"] = timedelta(seconds=poll_interval_sec)

        return await client.video.generate(**kwargs)

    async def _upload_tempfile_to_s3(
        self,
        ctx: Context,
        user: UserORM,
        *,
        tmp_path: str,
        s3_object_prefix: str,
        filename_prefix: str,
        ext: str,
    ) -> str:
        rand_name = "".join(random.choices(string.ascii_lowercase, k=8))
        upload_filename = f"{filename_prefix}_{rand_name}{ext}"

        user_files: Optional[UserFiles] = None
        upload_file: Optional[UploadFile] = None
        raw_file = None

        try:
            aws_s3 = get_public_s3(object_prefix=s3_object_prefix)
            user_files = UserFiles(user=user, s3=aws_s3)

            raw_file = open(tmp_path, "rb")
            upload_file = UploadFile(filename=upload_filename, file=raw_file)

            file_metas: list[FileMeta] = await user_files.upload_files(
                ctx=ctx,
                files=[upload_file],
            )

            if not file_metas:
                raise XAIVideoSaveError("S3 upload returned no file metadata.")

            return str(file_metas[0].url)

        except XAIVideoError:
            raise
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(
                f"[GROK-VIDEO] S3 upload failed error={exc}"
            )
            raise XAIVideoSaveError("Failed to upload generated video to S3") from exc
        finally:
            try:
                if upload_file is not None:
                    await upload_file.close()
            except Exception:
                pass

            try:
                if raw_file is not None and not raw_file.closed:
                    raw_file.close()
            except Exception:
                pass

    async def _fetch_https_to_tempfile(
        self,
        ctx: Context,
        url: str,
        *,
        timeout_sec: float,
        on_progress: ProgressCallback | None,
        max_bytes: Optional[int],
        suffix: str,
    ) -> tuple[str, Optional[str], int]:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise XAIVideoFetchError("Only https:// URLs are allowed for video fetch.")

        tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=suffix, delete=False)
        tmp_path = tmp.name
        tmp.close()

        bytes_read = 0
        total = 0
        content_type: Optional[str] = None

        try:
            timeout = httpx.Timeout(timeout_sec, connect=30.0)
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as h:
                async with h.stream("GET", url) as r:
                    r.raise_for_status()

                    final_url = str(r.url)
                    if urlparse(final_url).scheme != "https":
                        raise XAIVideoFetchError(
                            "Redirected to non-https URL which is not allowed."
                        )

                    content_type = r.headers.get("content-type")

                    cl = r.headers.get("content-length")
                    if cl and cl.isdigit():
                        total = int(cl)
                        if max_bytes is not None and total > max_bytes:
                            raise XAIVideoFetchError(
                                f"content-length={total} exceeds max_bytes={max_bytes}"
                            )

                    with open(tmp_path, "wb") as f:
                        async for chunk in r.aiter_bytes(chunk_size=4 * 1024 * 1024):
                            if not chunk:
                                continue

                            bytes_read += len(chunk)

                            if max_bytes is not None and bytes_read > max_bytes:
                                raise XAIVideoFetchError(
                                    f"Fetched bytes exceed max_bytes={max_bytes}"
                                )

                            f.write(chunk)

                            if on_progress:
                                on_progress(bytes_read, total)

            if on_progress and total == 0:
                on_progress(bytes_read, bytes_read)

            return tmp_path, content_type, bytes_read

        except XAIVideoError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        except Exception as exc:
            logger.bind(ctx=ctx).exception(f"[GROK-VIDEO] Failed to fetch url={url} error={exc}")
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise XAIVideoFetchError(f"Failed to fetch video from {url}") from exc

    @staticmethod
    def _extract_url(resp: Any) -> Optional[str]:
        for attr in ("url", "video_url", "download_url", "href"):
            value = getattr(resp, attr, None)
            if isinstance(value, str) and value:
                return value

        video = getattr(resp, "video", None)
        if video is not None:
            for attr in ("url", "video_url", "download_url", "href"):
                value = getattr(video, attr, None)
                if isinstance(value, str) and value:
                    return value

        nested_response = getattr(resp, "response", None)
        nested_video = getattr(nested_response, "video", None)
        if nested_video is not None:
            value = getattr(nested_video, "url", None)
            if isinstance(value, str) and value:
                return value

        return None

    @staticmethod
    def _extract_duration(resp: Any) -> Optional[int]:
        for source in (
            resp,
            getattr(resp, "video", None),
            getattr(getattr(resp, "response", None), "video", None),
        ):
            if source is None:
                continue
            value = getattr(source, "duration", None)
            if isinstance(value, int):
                return value
        return None

    @staticmethod
    def _extract_respect_moderation(resp: Any) -> Optional[bool]:
        for source in (
            resp,
            getattr(resp, "video", None),
            getattr(getattr(resp, "response", None), "video", None),
        ):
            if source is None:
                continue
            value = getattr(source, "respect_moderation", None)
            if isinstance(value, bool):
                return value
        return None

    @staticmethod
    def _extract_model(resp: Any) -> Optional[str]:
        for source in (resp, getattr(resp, "response", None)):
            if source is None:
                continue
            value = getattr(source, "model", None)
            if isinstance(value, str) and value:
                return value
        return None