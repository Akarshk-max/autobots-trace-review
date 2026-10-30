from pathlib import Path
from typing import Literal, Optional, Type

from openai.types import ImagesResponse
from pydantic import BaseModel
from pydantic_core import Url

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
from src.autobots.conn.openai.openai_images.image_model import ImageEdit
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState


class ImageEditInput(BaseModel):
    image: Url | Path | bytes | None = None
    prompt: str | None = None
    mask: Path | Url | bytes | None = None
    size: Optional[Literal["256x256", "512x512", "1024x1024"]] = None


class ImageEditOpenaiConfig(ImageEdit):
    provider: Literal["azure", "openai"]


class ActionImg2ImgEditOpenai(
    ActionABC[
        ImageEditOpenaiConfig,
        ImageEditOpenaiConfig,
        ImageEditOpenaiConfig,
        ImageEditInput,
        ImagesResponse,
    ]
):
    async def run_tool(
        self, action_config: ImageEditOpenaiConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> ImagesResponse | Exception:
        pass

    type = ActionType.img2img_edit_openai

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return ImageEditOpenaiConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return ImageEditOpenaiConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return ImageEditOpenaiConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return ImageEditInput

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return ImagesResponse

    # def __init__(self, action_config: ImageEdit, user: UserORM | None = None):
    #     super().__init__(action_config=action_config, user=user)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: ImageEditInput, notify: Notify | None = None
    ) -> ImagesResponse:
        if action_input.prompt:
            self.action_config.prompt = f"{self.action_config.prompt} {action_input.prompt}"
        if action_input.image:
            self.action_config.image = action_input.image
        if action_input.mask:
            self.action_config.mask = action_input.mask
        if action_input.size:
            self.action_config.size = action_input.size

        image_edit = ImageEdit(**self.action_config.model_dump(exclude_none=True))
        client = await AppConn.get_openai(
            ctx=ctx, db=self.db, user=self.user, provider=self.action_config.provider
        )
        images = await client.openai_images.create_image_edit(app_state=app_state, image_edit_input=image_edit)
        return images
