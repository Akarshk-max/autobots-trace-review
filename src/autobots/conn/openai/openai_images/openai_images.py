from pathlib import Path

import httpx
from loguru import logger
from openai import AsyncAzureOpenAI, AsyncOpenAI, BadRequestError
from openai.types import ImagesResponse
from pydantic_core import Url
from tenacity import retry, stop_after_attempt, wait_fixed

from src.autobots.conn.openai.openai_images.image_model import (
    ImageCreateVariation,
    ImageEdit,
    ImageReq,
)
from src.autobots.data_model.app_state import AppState

class OpenaiImages:
    def __init__(self, openai_client: AsyncAzureOpenAI | AsyncOpenAI):
        self.client = openai_client

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(3), reraise=True)
    async def create_image(
        self, app_state: AppState, image_req: ImageReq
    ) -> ImagesResponse | BadRequestError | Exception:
        try:
            logger.info("Starting OpenAI create image")
            res: ImagesResponse = await self.client.images.generate(
                **image_req.model_dump(exclude_none=True)
            )
            logger.info("Completed OpenAI create image")
            return res
        except Exception as e:
            logger.exception(f"Error OpenAI create image: {str(e)}")
            return e

    async def create_image_edit(self, app_state: AppState, image_edit_input: ImageEdit) -> ImagesResponse | None:
        try:
            # Converting image to path or bytes
            if isinstance(image_edit_input.image, Path):
                pass
            elif isinstance(image_edit_input.image, Url):
                resp = httpx.get(image_edit_input.image.unicode_string())
                image_edit_input.image = resp.content

            logger.info("Starting OpenAI create image edit")
            res: ImagesResponse = await self.client.images.edit(
                **image_edit_input.model_dump(exclude_none=True)
            )
            logger.info("Completed OpenAI create image edit")
            return res
        except Exception as e:
            logger.exception(f"Error OpenAI create image edit: {str(e)}")

    async def create_image_variation(
        self, app_state: AppState, image_create_variation_input: ImageCreateVariation
    ) -> ImagesResponse | None:
        try:
            # Converting image to path or bytes
            if isinstance(image_create_variation_input.image, Path):
                pass
            elif isinstance(image_create_variation_input.image, Url):
                resp = httpx.get(image_create_variation_input.image.unicode_string())
                image_create_variation_input.image = resp.content

            logger.info("Starting OpenAI create image variation")
            res: ImagesResponse = await self.client.images.create_variation(
                **image_create_variation_input.model_dump(exclude_none=True)
            )
            logger.info("Completed OpenAI create image variation")
            return res
        except Exception as e:
            logger.exception(f"Error OpenAI create image variation: {str(e)}")
