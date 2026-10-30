from typing import List, Optional, Type

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

# nano banana client + schema
from src.autobots.conn.gemini.gen_image.gemini_image_models import (
    NanoBananaImageInput,
    NanoBananaImageRequest,
    NanoBananaImageResult,
)
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class NanoBananaActionToolConfig(BaseModel):
    prompt: str = Field(..., min_length=1)
    urls: Optional[List[str]] = None
    model_config = ConfigDict(extra="forbid")


class ActionImageGenerateGemini(
    ActionABC[
        NanoBananaImageRequest,
        NanoBananaImageRequest,
        NanoBananaImageRequest,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.text2img_gemini

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return NanoBananaImageRequest

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return NanoBananaImageRequest

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return NanoBananaImageRequest

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
        Description
        -----------
        Generates one or more images using Google's Gemini image generation model
        based on a text prompt. Optional reference images may be provided to guide
        the style, composition, or subject of the generated image.

        Input Config
        ------------
        prompt : str
            A natural language description of the image to generate.
            This field is required and must be a non-empty string.

        images : list[NanoBananaImageInput] | None
            Optional list of reference images used to guide image generation.
            Each item must provide exactly one image source:
            - url
            - file_path
            - base64_content

            Reference images may be used for style transfer, visual grounding,
            or image-to-image generation. If omitted, the image is generated
            purely from the text prompt.

        Output
        ------
        DataBlocks
            A collection of output blocks produced by the action.
            This may include:
            - One or more image URLs pointing to generated images (hosted on S3)
            - Optional descriptive text returned by the model

            If image generation succeeds but produces no usable output,
            an internal server error is raised.
        """

    @staticmethod
    async def update_config_with_prev_results(
        ctx: Context,
        app_state: AppState,
        curr_config: NanoBananaImageRequest,
        prev_results: List[ActionResult] | None = None,
    ) -> NanoBananaImageRequest:
        return curr_config

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify: Optional[Notify] = None,
    ) -> DataBlocks | Exception:
        out = DataBlocks(blocks=[])

        try:
            client = await AppConn.get_gemini_image(ctx=ctx, db=self.db, user=self.user)

            req = self.action_config.model_copy(deep=True)

            prompt = action_input.get_llm_prompt(ctx=ctx) or req.prompt
            req.prompt = prompt

            images: List[NanoBananaImageInput] = []
            if action_input.urls:
                for u in action_input.urls:
                    try:
                        images.append(NanoBananaImageInput(url=u))
                    except Exception:
                        logger.bind(ctx=ctx, url=u).warning("Invalid url for image, skipping")

            if images:
                req.images = (req.images or []) + images

            result: NanoBananaImageResult = await client.generate_image(
                ctx=ctx,
                app_state=app_state,
                user=self.user,
                req=req,
                db=self.db,
                notify=notify,
            )

            for text in result.texts:
                if text.strip():
                    out.blocks.append(DataBlock(text=text.strip()))

            for url in result.s3_urls:
                out.blocks.append(DataBlock(urls=[url]))

            if not out.blocks:
                raise HTTPException(
                    status_code=500, detail="Gemini image generation produced no output"
                )

            return out

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"[GeminiImageAction] Error: {e}")
            return e

    async def run_tool(
        self,
        action_config: NanoBananaImageRequest,
        ctx: Context,
        app_state: AppState,
        notify: Optional[Notify] = None,
    ) -> DataBlocks | Exception:
        out = DataBlocks(blocks=[])

        try:
            if not action_config.prompt.strip():
                raise HTTPException(
                    status_code=400,
                    detail="prompt must be provided",
                )

            client = await AppConn.get_gemini_image(
                ctx=ctx,
                db=self.db,
                user=self.user,
            )

            # copy config models
            req = action_config.model_copy(deep=True)

            result: NanoBananaImageResult = await client.generate_image(
                ctx=ctx,
                app_state=app_state,
                user=self.user,
                req=req,
                db=self.db,
                notify=notify,
            )

            for text in result.texts:
                if text.strip():
                    out.blocks.append(DataBlock(text=text.strip()))

            for url in result.s3_urls:
                out.blocks.append(DataBlock(urls=[url]))

            if not out.blocks:
                raise HTTPException(
                    status_code=500,
                    detail="Gemini image generation produced no output",
                )

            return out

        except Exception as e:
            logger.bind(ctx=ctx).exception("[GeminiImageAction] Error: {}", e)
            return e
