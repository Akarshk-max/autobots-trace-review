from typing import List, Optional, Type

from fastapi import HTTPException
from loguru import logger

from src.autobots.action.action.action_doc_model import ActionResult
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import (
    ActionABC,
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
    ActionInputType,
    ActionOutputType,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.gemini.gen_video.gemini_video_models import (
    VeoMediaInput,
    VeoVideoRequest,
    VeoVideoResult,
)
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class ActionVideoGenerateGeminiVeo(
    ActionABC[
        VeoVideoRequest,
        VeoVideoRequest,
        VeoVideoRequest,
        DataBlock,
        DataBlocks,
    ]
):
    """
    Text/Image/Video - Video generation via Gemini Veo.
    Output is S3 URLs only (per VeoOutputConfig.return_bytes_b64=False).
    """

    type = ActionType.text2video_gemini_veo_video_generation

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return VeoVideoRequest

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return VeoVideoRequest

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return VeoVideoRequest

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
Generate one or more videos using Google Gemini Veo.

Required input
--------------
prompt (string, required)
A natural-language description of the video to generate.
Must be non-empty.
Example:
"A cinematic drone shot flying over a futuristic city at sunset"

Optional media inputs
---------------------
image (object, optional)
Use for image-to-video generation.
Provide exactly ONE of:
- url
- file_path
- base64_content

video (object, optional)
Use for video-to-video generation.
Provide exactly ONE of:
- gemini_uri
- url
- file_path
- base64_content

If both image and video are omitted, text-to-video generation is used.

Model selection
---------------
model (enum, optional, default: veo-3.1-generate-preview)
Available values:
- veo-3.1-generate-preview
- veo-3.1-fast-generate-preview
- veo-2.0-generate-001

Generation configuration (generation_config)
---------------------------------------------
All fields are optional.

Common fields:
- number_of_videos (int, 1-4, default 1)
- duration_seconds (int, 1-60)
- fps (int, 1-60)
- aspect_ratio ("16:9" | "9:16")
- resolution ("720p" | "1080p")
- generate_audio (bool)

Prompt control:
- negative_prompt (string, optional)
- enhance_prompt (bool, optional)
  IMPORTANT:
  - Only supported for model veo-2.0-generate-001
  - Automatically ignored for Veo 3.x models

Advanced inputs:
- reference_images (list, optional)
  - Each item requires:
    - reference_id (unique integer)
    - image (url / file_path / base64_content only)
    - reference_type ("REFERENCE_TYPE_ASSET" | "REFERENCE_TYPE_STYLE")
  - For Veo 3.1, use at most 3 reference images

- last_frame (object, optional)
  - Same format as image
  - Must NOT use gemini_uri

- mask (object, optional)
  - mask_mode ("INSERT" | "OUTPAINT" | "REMOVE" | "REMOVE_STATIC")
  - mask_image (url / file_path / base64_content only)

Output behavior
---------------
- Videos are uploaded to S3
- The tool returns S3 URLs only
- Base64 video bytes are never returned

Output
------
Returns a DataBlocks object containing:
- One or more blocks with S3 video URLs
- Optional text blocks with provider_uri for debugging

Failure conditions
------------------
The tool fails only if:
- prompt is missing or empty
- Gemini Veo client is unavailable
- Video generation completes but produces no output

All unsupported or incompatible fields are ignored when possible.
        """

    @staticmethod
    async def update_config_with_prev_results(
        ctx: Context,
        app_state: AppState,
        curr_config: VeoVideoRequest,
        prev_results: List[ActionResult] | None = None,
    ) -> VeoVideoRequest:
        return curr_config

    def _infer_media_from_urls(self, urls: List[str]) -> tuple[Optional[VeoMediaInput], Optional[VeoMediaInput]]:
        """
        Best-effort heuristic:
        - first image/* URL becomes req.image if not set
        - first video/* URL becomes req.video if not set
        If mime isn't known, we still accept it and let Veo client validate if it can.
        """
        image: Optional[VeoMediaInput] = None
        video: Optional[VeoMediaInput] = None

        for u in urls:
            if not u:
                continue
            su = str(u).strip()
            if not su:
                continue

            # very light heuristics by extension
            lower = su.split("?")[0].lower()
            is_img = lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff"))
            is_vid = lower.endswith((".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"))

            if is_img and image is None:
                try:
                    image = VeoMediaInput(url=su)
                except Exception:
                    logger.bind(url=su).warning("Invalid image url, skipping")
                continue

            if is_vid and video is None:
                try:
                    video = VeoMediaInput(url=su)
                except Exception:
                    logger.bind(url=su).warning("Invalid video url, skipping")
                continue

        return image, video

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify: Optional[Notify] = None,
    ) -> DataBlocks | Exception:
        out = DataBlocks(blocks=[])

        try:
            client = await AppConn.get_gemini_veo_video(ctx=ctx, db=self.db, user=self.user)
            if not client:
                raise HTTPException(status_code=500, detail="Gemini Veo client not available")

            req = self.action_config.model_copy(deep=True)

            prompt = action_input.get_llm_prompt(ctx=ctx) or req.prompt
            if not prompt or not prompt.strip():
                raise HTTPException(status_code=400, detail="prompt must be provided")
            req.prompt = prompt.strip()

            # If caller provided urls, infer image/video if not explicitly set in config
            if action_input.urls:
                inf_img, inf_vid = self._infer_media_from_urls(action_input.urls)
                if req.image is None and inf_img is not None:
                    req.image = inf_img
                if req.video is None and inf_vid is not None:
                    req.video = inf_vid

            result: VeoVideoResult = await client.generate_video(
                ctx=ctx,
                user=self.user,
                req=req,
                db=self.db,
                notify=notify,
            )

            # Emit URLs
            for url in result.s3_urls:
                if url and str(url).strip():
                    out.blocks.append(DataBlock(urls=[str(url)]))

            # Optionally emit provider uris as text blocks for debugging/trace
            for uri in result.provider_uris:
                if uri and str(uri).strip():
                    out.blocks.append(DataBlock(text=f"provider_uri: {uri}"))

            if not out.blocks:
                raise HTTPException(status_code=500, detail="Veo video generation produced no output")

            return out

        except Exception as e:
            logger.bind(ctx=ctx).exception("[VeoVideoAction] Error: {}", e)
            return e

    async def run_tool(
        self,
        action_config: VeoVideoRequest,
        ctx: Context,
        app_state: AppState,
        notify: Optional[Notify] = None,
    ) -> DataBlocks | Exception:
        out = DataBlocks(blocks=[])

        try:
            if not action_config.prompt or not action_config.prompt.strip():
                raise HTTPException(status_code=400, detail="prompt must be provided")

            client = await AppConn.get_gemini_veo_video(ctx=ctx, db=self.db, user=self.user)
            if not client:
                raise HTTPException(status_code=500, detail="Gemini Veo client not available")

            req = action_config.model_copy(deep=True)

            result: VeoVideoResult = await client.generate_video(
                ctx=ctx,
                user=self.user,
                req=req,
                db=self.db,
                notify=notify,
            )

            for url in result.s3_urls:
                if url and str(url).strip():
                    out.blocks.append(DataBlock(urls=[str(url)]))

            for uri in result.provider_uris:
                if uri and str(uri).strip():
                    out.blocks.append(DataBlock(text=f"provider_uri: {uri}"))

            if not out.blocks:
                raise HTTPException(status_code=500, detail="Veo video generation produced no output")

            return out

        except Exception as e:
            logger.bind(ctx=ctx).exception("[VeoVideoAction] Error: {}", e)
            return e