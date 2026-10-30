from typing import List, Optional, Type

import xai_sdk
from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

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
from src.autobots.conn.grok.grok_video.grok_video import XAIVideoManager
from src.autobots.conn.grok.grok_video.grok_video_models import (
    GrokVideoModel,
    VideoAspectRatioType,
    VideoResolutionType,
)
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class GrokVideoActionConfig(BaseModel):
    prompt: str = Field(..., min_length=1)

    model: GrokVideoModel = GrokVideoModel.grok_imagine_video
    duration: Optional[int] = Field(default=None, ge=1, le=15)

    aspect_ratio: VideoAspectRatioType | None = None
    resolution: VideoResolutionType | None = None

    image_url: Optional[str] = None
    video_url: Optional[str] = None

    fetch_timeout_sec: float = 300.0
    max_fetch_bytes: Optional[int] = None

    s3_object_prefix: str = "grok-video"
    filename_prefix: str = "video"
    ext: str = ".mp4"

    poll_timeout_sec: Optional[float] = Field(default=None, gt=0)
    poll_interval_sec: Optional[float] = Field(default=None, gt=0)

    model_config = ConfigDict(extra="forbid")


class ActionVideoGenerateGrok(
    ActionABC[
        GrokVideoActionConfig,
        GrokVideoActionConfig,
        GrokVideoActionConfig,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.text2video_grok

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return GrokVideoActionConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return GrokVideoActionConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return GrokVideoActionConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    async def update_config_with_prev_results(
        ctx: Context,
        app_state: AppState,
        curr_config: GrokVideoActionConfig,
        prev_results: List[ActionResult] | None = None,
    ) -> GrokVideoActionConfig:
        return curr_config

    @staticmethod
    def get_description() -> str:
        return """
        Description
        -----------
        Generates a video using xAI Grok video model and uploads the output to S3.

        Input
        -----
        prompt : str
            Text prompt to generate the video.
        image_url : Optional[str]
            Optional source image URL for image-to-video generation.
        video_url : Optional[str]
            Optional source video URL for video editing.

        Output
        ------
        DataBlocks:
            blocks containing urls=[...]
        """

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify: Optional[Notify] = None,
    ) -> DataBlocks | Exception:
        out = DataBlocks(blocks=[])

        try:
            req_cfg = self.action_config.model_copy(deep=True)
            prompt = action_input.get_llm_prompt(ctx=ctx) or req_cfg.prompt

            if not prompt or not prompt.strip():
                raise HTTPException(status_code=400, detail="prompt must be provided")

            if req_cfg.image_url and req_cfg.video_url:
                raise HTTPException(
                    status_code=400,
                    detail="Only one of image_url or video_url may be provided",
                )

            grok_conn = await AppConn.get_grok(ctx=ctx, db=self.db, user=self.user)
            client: xai_sdk.AsyncClient = grok_conn.client

            mgr = XAIVideoManager()

            resp = await mgr.generate(
                ctx=ctx,
                user=self.user,
                db=self.db,
                client=client,
                prompt=prompt.strip(),
                model=req_cfg.model,
                duration=req_cfg.duration,
                aspect_ratio=req_cfg.aspect_ratio,
                resolution=req_cfg.resolution,
                image_url=req_cfg.image_url,
                video_url=req_cfg.video_url,
                fetch_timeout_sec=req_cfg.fetch_timeout_sec,
                max_fetch_bytes=req_cfg.max_fetch_bytes,
                s3_object_prefix=req_cfg.s3_object_prefix,
                filename_prefix=req_cfg.filename_prefix,
                ext=req_cfg.ext,
                poll_timeout_sec=req_cfg.poll_timeout_sec,
                poll_interval_sec=req_cfg.poll_interval_sec,
            )

            urls: List[str] = [video.url for video in resp.videos if video.url]

            if not urls:
                raise HTTPException(
                    status_code=500,
                    detail="Grok video generation produced no output",
                )

            out.blocks.append(DataBlock(urls=urls))

            if resp.warnings:
                out.blocks.append(DataBlock(text="; ".join(resp.warnings)))

            return out

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"[GrokVideoAction] Error: {e}")
            return e

    async def run_tool(
        self,
        action_config: GrokVideoActionConfig,
        ctx: Context,
        app_state: AppState,
        notify: Optional[Notify] = None,
    ) -> DataBlocks | Exception:
        out = DataBlocks(blocks=[])

        try:
            if not action_config.prompt or not action_config.prompt.strip():
                raise HTTPException(status_code=400, detail="prompt must be provided")

            if action_config.image_url and action_config.video_url:
                raise HTTPException(
                    status_code=400,
                    detail="Only one of image_url or video_url may be provided",
                )

            grok_conn = await AppConn.get_grok(ctx=ctx, db=self.db, user=self.user)
            client: xai_sdk.AsyncClient = grok_conn.client

            mgr = XAIVideoManager()

            resp = await mgr.generate(
                ctx=ctx,
                user=self.user,
                db=self.db,
                client=client,
                prompt=action_config.prompt.strip(),
                model=action_config.model,
                duration=action_config.duration,
                aspect_ratio=action_config.aspect_ratio,
                resolution=action_config.resolution,
                image_url=action_config.image_url,
                video_url=action_config.video_url,
                fetch_timeout_sec=action_config.fetch_timeout_sec,
                max_fetch_bytes=action_config.max_fetch_bytes,
                s3_object_prefix=action_config.s3_object_prefix,
                filename_prefix=action_config.filename_prefix,
                ext=action_config.ext,
                poll_timeout_sec=action_config.poll_timeout_sec,
                poll_interval_sec=action_config.poll_interval_sec,
            )

            urls: List[str] = [video.url for video in resp.videos if video.url]

            if not urls:
                raise HTTPException(
                    status_code=500,
                    detail="Grok video generation produced no output",
                )

            out.blocks.append(DataBlock(urls=urls))

            if resp.warnings:
                out.blocks.append(DataBlock(text="; ".join(resp.warnings)))

            return out

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"[GrokVideoAction] Error: {e}")
            return e