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
from src.autobots.conn.grok.grok_image.grok_image import XAIImageManager
from src.autobots.conn.grok.grok_image.image_models import GrokImageModel, ImageFormatType
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class GrokImageActionConfig(BaseModel):
    prompt: str = Field(..., min_length=1)

    model: GrokImageModel = GrokImageModel.grok_2_image_1212
    n: int = Field(default=1, ge=1, le=10)

    image_format: ImageFormatType = Field(default="url")
    fetch_bytes_if_url: bool = True
    include_image_bytes: bool = False

    fetch_timeout_sec: float = 60.0
    max_fetch_bytes: Optional[int] = None

    save_to_s3: bool = True
    urls_only: bool = True

    s3_object_prefix: str = "grok-image"
    filename_prefix: str = "image"
    ext: str = ".jpg"

    model_config = ConfigDict(extra="forbid")


class ActionImageGenerateGrok(
    ActionABC[
        GrokImageActionConfig, GrokImageActionConfig, GrokImageActionConfig, DataBlock, DataBlocks
    ]
):
    type = ActionType.text2img_grok

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return GrokImageActionConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return GrokImageActionConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return GrokImageActionConfig

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
        curr_config: GrokImageActionConfig,
        prev_results: List[ActionResult] | None = None,
    ) -> GrokImageActionConfig:
        return curr_config

    @staticmethod
    def get_description() -> str:
        return """
        Description
        -----------
        Generates one or more images using xAI Grok image model (grok-2-image).
        The action returns image URLs

        Input
        -----
        prompt : str
            Text prompt to generate image.

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

            grok_conn = await AppConn.get_grok(ctx=ctx, db=self.db, user=self.user)
            client: xai_sdk.AsyncClient = grok_conn.client

            mgr = XAIImageManager()

            resp = await mgr.generate(
                ctx=ctx,
                app_state=app_state,
                user=self.user,
                db=self.db,
                client=client,
                prompt=prompt.strip(),
                model=req_cfg.model,
                n=req_cfg.n,
                image_format=req_cfg.image_format,
                fetch_bytes_if_url=req_cfg.fetch_bytes_if_url,
                include_image_bytes=req_cfg.include_image_bytes,
                fetch_timeout_sec=req_cfg.fetch_timeout_sec,
                max_fetch_bytes=req_cfg.max_fetch_bytes,
                save_to_s3=req_cfg.save_to_s3,
                urls_only=req_cfg.urls_only,
                s3_object_prefix=req_cfg.s3_object_prefix,
                filename_prefix=req_cfg.filename_prefix,
                ext=req_cfg.ext,
            )

            urls: List[str] = []
            for img in resp.images:
                if img.url:
                    urls.append(img.url)

            if not urls:
                for img in resp.images:
                    if getattr(img, "provider_url", None):
                        urls.append(img.provider_url)

            if not urls:
                raise HTTPException(
                    status_code=500, detail="Grok image generation produced no output"
                )

            out.blocks.append(DataBlock(urls=urls))

            if resp.warnings:
                out.blocks.append(DataBlock(text="; ".join(resp.warnings)))

            return out

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"[GrokImageAction] Error: {e}")
            return e

    async def run_tool(
        self,
        action_config: GrokImageActionConfig,
        ctx: Context,
        app_state: AppState,
        notify: Optional[Notify] = None,
    ) -> DataBlocks | Exception:
        out = DataBlocks(blocks=[])

        try:
            if not action_config.prompt or not action_config.prompt.strip():
                raise HTTPException(status_code=400, detail="prompt must be provided")

            grok_conn = await AppConn.get_grok(ctx=ctx, db=self.db, user=self.user)
            client: xai_sdk.AsyncClient = grok_conn.client

            mgr = XAIImageManager()

            resp = await mgr.generate(
                ctx=ctx,
                app_state=app_state,
                user=self.user,
                db=self.db,
                client=client,
                prompt=action_config.prompt.strip(),
                model=action_config.model,
                n=action_config.n,
                image_format=action_config.image_format,
                fetch_bytes_if_url=action_config.fetch_bytes_if_url,
                include_image_bytes=action_config.include_image_bytes,
                fetch_timeout_sec=action_config.fetch_timeout_sec,
                max_fetch_bytes=action_config.max_fetch_bytes,
                save_to_s3=action_config.save_to_s3,
                urls_only=action_config.urls_only,
                s3_object_prefix=action_config.s3_object_prefix,
                filename_prefix=action_config.filename_prefix,
                ext=action_config.ext,
            )

            urls: List[str] = [img.url for img in resp.images if img.url]
            if not urls:
                for img in resp.images:
                    if getattr(img, "provider_url", None):
                        urls.append(img.provider_url)
            if not urls:
                raise HTTPException(
                    status_code=500, detail="Grok image generation produced no output"
                )

            out.blocks.append(DataBlock(urls=urls))
            if resp.warnings:
                out.blocks.append(DataBlock(text="; ".join(resp.warnings)))

            return out

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"[GrokImageAction] Error: {e}")
            return e
