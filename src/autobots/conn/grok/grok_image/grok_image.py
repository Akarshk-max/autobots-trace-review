import asyncio
import io
import random
import string
from typing import Any, Callable, Optional

import xai_sdk
from fastapi import UploadFile
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.grok.grok_image.image_models import (
    GeneratedImage,
    GrokImageModel,
    ImageFormatType,
    ImageGenerationRequest,
    ImageGenerationResponse,
    XAIImageError,
    XAIImageFetchError,
    XAIImageGenerateError,
    XAIImageSaveError,
)
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.files.user_files_model import FileMeta
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

ProgressCallback = Callable[[int, int], None]


class XAIImageManager:
    """
    Wrapper for image generation using xai sdk client maybe
    - Final output URL is S3 public URL.
    - Always obtains bytes.
    """

    # this method only generate images for a single prompt
    async def generate(
        self,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        *,
        prompt: str,
        model: GrokImageModel = GrokImageModel.grok_2_image_1212,
        n: int = 1,
        image_format: ImageFormatType = "url",
        fetch_bytes_if_url: bool = True,
        include_image_bytes: bool = True,
        fetch_timeout_sec: float = 60.0,
        on_fetch_progress: ProgressCallback | None = None,
        max_fetch_bytes: Optional[int] = None,
        save_to_s3: bool = False,
        urls_only: bool = False,
        s3_object_prefix: str = "grok-image",
        filename_prefix: str = "image",
        ext: str = ".jpg",
    ) -> ImageGenerationResponse:
        if urls_only:
            include_image_bytes = False

        req = ImageGenerationRequest(
            prompt=prompt,
            model=model,
            n=n,
            image_format=image_format,
            fetch_bytes_if_url=fetch_bytes_if_url,
            include_image_bytes=include_image_bytes,
            save_to_s3=save_to_s3,
            urls_only=urls_only,
        )

        try:
            responses = await self._call_xai(
                client,
                prompt=prompt,
                model=model.value,
                n=n,
                image_format=image_format,
            )

            images: list[GeneratedImage] = []
            warnings: list[str] = []

            for i, resp in enumerate(responses):
                gi = await self._to_generated_image(
                    ctx=ctx,
                    idx=i,
                    resp=resp,
                    image_format=image_format,
                    fetch_bytes_if_url=fetch_bytes_if_url,
                    fetch_timeout_sec=fetch_timeout_sec,
                    on_fetch_progress=on_fetch_progress,
                    max_fetch_bytes=max_fetch_bytes,
                    warnings=warnings,
                )
                images.append(gi)

            if save_to_s3:
                upload_files: list[UploadFile] = []
                upload_map: list[int] = []

                for i, gi in enumerate(images):
                    if not gi.image_bytes:
                        raise XAIImageSaveError(
                            f"No image_bytes available for S3 upload (index={i}). "
                            f"Use image_format='base64' or enable fetch_bytes_if_url=True with image_format='url'."
                        )

                    binary = io.BytesIO(gi.image_bytes)
                    binary.seek(0)
                    rand_name = "".join(random.choices(string.ascii_lowercase, k=8))
                    upload_files.append(
                        UploadFile(
                            filename=f"{filename_prefix}_{gi.index}_{rand_name}_{ext}",
                            file=binary,
                        )
                    )
                    upload_map.append(i)

                aws_s3 = get_public_s3(object_prefix=s3_object_prefix)
                user_files = UserFiles(user=user, s3=aws_s3)

                try:
                    file_metas: list[FileMeta] = await user_files.upload_files(
                        ctx=ctx, files=upload_files
                    )
                except Exception as exc:
                    logger.bind(ctx=ctx, user=user).exception(
                        f"[GROK-IMAGE] S3 upload failed error={exc}"
                    )
                    raise XAIImageSaveError("Failed to upload generated image(s) to S3") from exc

                for meta, img_idx in zip(file_metas, upload_map):
                    images[img_idx].url = str(meta.url)

                    if urls_only:
                        images[img_idx].image_bytes = None
                        images[img_idx].size_bytes = None
                        images[img_idx].provider_url = None
                    elif not include_image_bytes:
                        images[img_idx].image_bytes = None
                        images[img_idx].size_bytes = None

                logger.bind(ctx=ctx, user=user).info(
                    f"[GROK-IMAGE] Generated n={len(images)} model={model} format={image_format} -> uploaded_to_s3"
                )
                return ImageGenerationResponse(request=req, images=images, warnings=warnings)

            for gi in images:
                gi.url = gi.provider_url
                if urls_only or (not include_image_bytes):
                    gi.image_bytes = None
                    gi.size_bytes = None

            logger.bind(ctx=ctx, user=user).info(
                f"[GROK-IMAGE] Generated n={len(images)} model={model} format={image_format}"
            )
            return ImageGenerationResponse(request=req, images=images, warnings=warnings)

        except XAIImageError:
            raise
        except Exception as exc:
            logger.bind(ctx=ctx, user=user).exception(f"[GROK-IMAGE] Failed generate error={exc}")
            raise XAIImageGenerateError("Failed to generate image(s)") from exc

    # this methods takes list of promts and generate images
    async def generate_many_prompts(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        client: xai_sdk.AsyncClient,
        *,
        prompts: list[str],
        concurrency: int = 3,
        model: str = "grok-2-image",
        n_per_prompt: int = 1,
        image_format: ImageFormatType = "url",
        fetch_bytes_if_url: bool = True,
        include_image_bytes: bool = True,
        fetch_timeout_sec: float = 60.0,
        max_fetch_bytes: Optional[int] = None,
        on_prompt_complete: Callable[[int, str, object], None] | None = None,
        save_to_s3: bool = False,
        urls_only: bool = False,
        s3_object_prefix: str = "grok-image",
        filename_prefix: str = "image",
        ext: str = ".jpg",
    ) -> dict[str, ImageGenerationResponse]:
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
                        n=n_per_prompt,
                        image_format=image_format,
                        fetch_bytes_if_url=fetch_bytes_if_url,
                        include_image_bytes=include_image_bytes,
                        fetch_timeout_sec=fetch_timeout_sec,
                        max_fetch_bytes=max_fetch_bytes,
                        save_to_s3=save_to_s3,
                        urls_only=urls_only,
                        s3_object_prefix=s3_object_prefix,
                        filename_prefix=filename_prefix,
                        ext=ext,
                    )
                    results[p] = out
                    if on_prompt_complete:
                        on_prompt_complete(idx, p, out)
                except Exception as exc:
                    results[p] = exc
                    if on_prompt_complete:
                        on_prompt_complete(idx, p, exc)

        await asyncio.gather(*(asyncio.create_task(_one(i, p)) for i, p in enumerate(prompts)))

        out: dict[str, ImageGenerationResponse] = {}
        for p, r in results.items():
            if isinstance(r, BaseException):
                continue
            out[p] = r  # type: ignore[assignment]
        return out

    # generate image from xai client - https://docs.x.ai/docs/guides/image-generations
    async def _call_xai(
        self,
        client: xai_sdk.AsyncClient,
        *,
        prompt: str,
        model: str,
        n: int,
        image_format: ImageFormatType,
    ) -> list[Any]:
        if n == 1:
            r = await client.image.sample(prompt, model=model, image_format=image_format)
            return [r]
        return await client.image.sample_batch(prompt, n=n, model=model, image_format=image_format)

    async def _to_generated_image(
        self,
        ctx: Context,
        idx: int,
        resp: Any,
        *,
        image_format: ImageFormatType,
        fetch_bytes_if_url: bool,
        fetch_timeout_sec: float,
        on_fetch_progress: ProgressCallback | None,
        max_fetch_bytes: Optional[int],
        warnings: list[str],
    ) -> GeneratedImage:
        revised_prompt = getattr(resp, "prompt", None)
        provider_url = self._extract_url(resp)

        image_bytes: Optional[bytes] = None
        content_type: Optional[str] = None

        if image_format == "base64":
            image_bytes = await self._try_get_image_bytes(resp)
        else:
            if fetch_bytes_if_url:
                image_bytes = await self._try_get_image_bytes(resp)

            if (not image_bytes) and provider_url and fetch_bytes_if_url:
                image_bytes, content_type = await self._fetch_https_bytes(
                    ctx=ctx,
                    url=provider_url,
                    timeout_sec=fetch_timeout_sec,
                    on_progress=on_fetch_progress,
                    max_bytes=max_fetch_bytes,
                )

        if image_format == "url" and not provider_url:
            warnings.append(f"SDK response missing url for image index={idx}.")

        if fetch_bytes_if_url and not image_bytes:
            warnings.append(f"Could not obtain bytes for image index={idx}.")

        return GeneratedImage(
            index=idx,
            revised_prompt=revised_prompt,
            provider_url=provider_url,
            url=None,
            image_bytes=image_bytes,
            content_type=content_type,
            size_bytes=(len(image_bytes) if image_bytes else None),
        )

    @staticmethod
    def _extract_url(resp: Any) -> Optional[str]:
        url = getattr(resp, "url", None)
        if isinstance(url, str) and url:
            return url

        for key in ("image_url", "download_url", "href"):
            v = getattr(resp, key, None)
            if isinstance(v, str) and v:
                return v
        return None

    @staticmethod
    async def _try_get_image_bytes(resp: Any) -> Optional[bytes]:
        """
        Official xAI example uses: await resp.image
        https://docs.x.ai/docs/guides/image-generations
        Handles:
        - resp.image is awaitable
        - resp.image is bytes
        - resp.image() returns awaitable
        """
        img = getattr(resp, "image", None)
        if img is None:
            return None

        try:
            if isinstance(img, (bytes, bytearray)):
                return bytes(img)

            if asyncio.iscoroutine(img) or hasattr(img, "__await__"):
                data = await img
                return bytes(data) if isinstance(data, (bytes, bytearray)) else None

            if callable(img):
                maybe = img()
                if asyncio.iscoroutine(maybe) or hasattr(maybe, "__await__"):
                    data = await maybe
                    return bytes(data) if isinstance(data, (bytes, bytearray)) else None

            return None
        except Exception:
            return None

    async def _fetch_https_bytes(
        self,
        ctx: Context,
        url: str,
        *,
        timeout_sec: float,
        on_progress: ProgressCallback | None,
        max_bytes: Optional[int],
    ) -> tuple[bytes, Optional[str]]:
        import os
        import tempfile
        from urllib.parse import urlparse

        import httpx

        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise XAIImageFetchError("Only https:// URLs are allowed for image fetch.")

        tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=".img", delete=False)
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
                        raise XAIImageFetchError(
                            "Redirected to non-https URL which is not allowed."
                        )

                    content_type = r.headers.get("content-type")

                    cl = r.headers.get("content-length")
                    if cl and cl.isdigit():
                        total = int(cl)
                        if max_bytes is not None and total > max_bytes:
                            raise XAIImageFetchError(
                                f"content-length={total} exceeds max_bytes={max_bytes}"
                            )

                    with open(tmp_path, "wb") as f:
                        async for chunk in r.aiter_bytes(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            bytes_read += len(chunk)

                            if max_bytes is not None and bytes_read > max_bytes:
                                raise XAIImageFetchError(
                                    f"Fetched bytes exceed max_bytes={max_bytes}"
                                )

                            f.write(chunk)
                            if on_progress:
                                on_progress(bytes_read, total)

            if on_progress and total == 0:
                on_progress(bytes_read, bytes_read)

            with open(tmp_path, "rb") as f:
                return f.read(), content_type

        except Exception as exc:
            logger.bind(ctx=ctx).exception(f"[GROK-IMAGE] Failed to fetch url={url} error={exc}")
            raise XAIImageFetchError(f"Failed to fetch image bytes from {url}") from exc
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
