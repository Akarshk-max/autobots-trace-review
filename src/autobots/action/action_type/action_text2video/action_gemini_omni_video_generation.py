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
from src.autobots.conn.gemini.gen_video_omni.gemini_omni_models import (
    OmniMediaInput,
    OmniVideoRequest,
    OmniVideoResult,
)
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.server_sent_events.server_sent_events import Notify

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".heic", ".heif")
VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".mpeg", ".mpg", ".3gp")


class ActionVideoGenerateGeminiOmni(
    ActionABC[
        OmniVideoRequest,
        OmniVideoRequest,
        OmniVideoRequest,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.text2video_gemini_omni_video_generation

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return OmniVideoRequest

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return OmniVideoRequest

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return OmniVideoRequest

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
Generate or edit a video using Google Gemini Omni Flash (gemini-omni-1.1-flash).

Required input
--------------
prompt (string, required)
A natural-language description of the video to generate, or of the edit to apply.
Example:
"A marble rolling fast on a chain reaction style track"

Optional media inputs
---------------------
image (object, optional)
Use for image-to-video generation. Provide exactly ONE of:
- url
- file_path
- base64_content
- gemini_uri

video (object, optional)
Use to edit or extend an existing video (10 seconds or less). Provide exactly ONE of:
- url
- file_path
- base64_content
- gemini_uri

If both image and video are omitted, text-to-video generation is used.

Conversational editing
----------------------
previous_interaction_id (string, optional)
Every run returns an interaction_id. Pass it back here with a new prompt to edit
the previously generated video while keeping everything else the same.
Example: "Make the violin invisible."

Generation configuration (generation_config)
---------------------------------------------
All fields are optional.
- aspect_ratio ("16:9" | "9:16")
- resolution ("360p" | "720p" | "1080p" | "4k")
- duration_seconds (int, 3-10)
- task ("text_to_video" | "image_to_video" | "reference_to_video" | "edit" | "extend")
  If omitted, the model picks the task from the prompt and media.
- request_timeout_seconds (float, default 900)

Output
------
Returns a DataBlocks object containing:
- One block with the S3 video URL
- One text block "interaction_id: <id>" for conversational editing
- One text block "provider_uri: <uri>" for debugging

Failure conditions
------------------
- prompt is missing or empty
- Gemini Omni client is unavailable
- Video generation completes but produces no output
        """

    @staticmethod
    async def update_config_with_prev_results(
        ctx: Context,
        app_state: AppState,
        curr_config: OmniVideoRequest,
        prev_results: List[ActionResult] | None = None,
    ) -> OmniVideoRequest:
        return curr_config

    @staticmethod
    def _infer_media_from_urls(urls: List[str]) -> tuple[Optional[OmniMediaInput], Optional[OmniMediaInput]]:
        image: Optional[OmniMediaInput] = None
        video: Optional[OmniMediaInput] = None
        for url in urls:
            candidate = str(url or "").strip()
            if not candidate:
                continue
            lower = candidate.split("?")[0].lower()
            if image is None and lower.endswith(IMAGE_EXTENSIONS):
                image = OmniMediaInput(url=candidate)
            elif video is None and lower.endswith(VIDEO_EXTENSIONS):
                video = OmniMediaInput(url=candidate)
        return image, video

    @staticmethod
    def _to_output(result: OmniVideoResult) -> DataBlocks:
        out = DataBlocks(blocks=[])
        for url in result.s3_urls:
            if url and str(url).strip():
                out.blocks.append(DataBlock(urls=[str(url)]))
        if result.interaction_id:
            out.blocks.append(DataBlock(text=f"interaction_id: {result.interaction_id}"))
        for uri in result.provider_uris:
            if uri and str(uri).strip():
                out.blocks.append(DataBlock(text=f"provider_uri: {uri}"))
        if not out.blocks:
            raise HTTPException(status_code=500, detail="Omni video generation produced no output")
        return out

    async def _generate(self, ctx: Context, req: OmniVideoRequest, notify: Optional[Notify]) -> DataBlocks:
        client = await AppConn.get_gemini_omni_video(ctx=ctx, db=self.db, user=self.user)
        if not client or isinstance(client, Exception):
            raise HTTPException(status_code=500, detail="Gemini Omni client not available")
        result: OmniVideoResult = await client.generate_video(
            ctx=ctx,
            user=self.user,
            req=req,
            db=self.db,
            notify=notify,
        )
        return self._to_output(result)

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify: Optional[Notify] = None,
    ) -> DataBlocks | Exception:
        try:
            req = self.action_config.model_copy(deep=True)

            prompt = action_input.get_llm_prompt(ctx=ctx) or req.prompt
            if not prompt or not prompt.strip():
                raise HTTPException(status_code=400, detail="prompt must be provided")
            req.prompt = prompt.strip()

            if action_input.urls:
                inferred_image, inferred_video = self._infer_media_from_urls(action_input.urls)
                if req.image is None and inferred_image is not None:
                    req.image = inferred_image
                if req.video is None and inferred_video is not None:
                    req.video = inferred_video

            return await self._generate(ctx, req, notify)
        except Exception as e:
            logger.bind(ctx=ctx).exception("[OmniVideoAction] Error: {}", e)
            return e

    async def run_tool(
        self,
        action_config: OmniVideoRequest,
        ctx: Context,
        app_state: AppState,
        notify: Optional[Notify] = None,
    ) -> DataBlocks | Exception:
        try:
            if not action_config.prompt or not action_config.prompt.strip():
                raise HTTPException(status_code=400, detail="prompt must be provided")
            return await self._generate(ctx, action_config.model_copy(deep=True), notify)
        except Exception as e:
            logger.bind(ctx=ctx).exception("[OmniVideoAction] Error: {}", e)
            return e
