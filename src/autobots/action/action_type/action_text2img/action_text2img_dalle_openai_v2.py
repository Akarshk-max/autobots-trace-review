from typing import Literal, Type

from fastapi import HTTPException
from loguru import logger
from openai import BadRequestError

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
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.openai.openai_images.image_model import ImageReq
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

# class ActionCreateGenImageDalleOpenai(ActionCreate):
#     type: ActionType = ActionType.text2img_dalle_openai
#     config: ImageReq
#     input: Optional[TextObj] = None
#     output: Optional[ImagesResponse] = None


class DalleOpenaiConfig(ImageReq):
    provider: Literal["azure", "openai"]


class ActionGenImageDalleOpenAiV2(
    ActionABC[DalleOpenaiConfig, DalleOpenaiConfig, DalleOpenaiConfig, DataBlock, DataBlocks]
):
    type = ActionType.text2img_dalle_openai

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return DalleOpenaiConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return DalleOpenaiConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return DalleOpenaiConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return (
            "Generates images using OpenAI/Azure image APIs from a text prompt. "
            "The tool accepts an image-generation configuration (prompt plus optional "
            "generation parameters), invokes the provider's image creation endpoint, "
            "stores or proxies resulting image URLs to user-accessible storage when "
            "possible, and returns the images as DataBlocks.\n\n"
            "Input ( config ):\n"
            "  {\n"
            '      "prompt": "A red sports car on a coastal road", # required\n'
            "  }\n\n"
            "  Runtime behavior:\n"
            "    - If the action receives runtime text (via action input), that text is\n"
            "      appended to the configured prompt before generation.\n"
            "    - The tool will attempt to upload provider-returned images to the\n"
            "      project's public S3 (or proxy them) and return the uploaded URLs when\n"
            "      upload succeeds; otherwise provider URLs are returned.\n\n"
            "Output:\n"
            "  A DataBlocks object where each DataBlock represents one generated image and\n"
            "  contains at minimum:\n"
            "    - text: the (possibly revised) prompt used by the provider\n"
            "    - urls: a list containing the public URL for the generated image\n\n"
            "  On error (bad request, provider error, or unexpected exception), the\n"
            "  exception is returned."
        )

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            openai_client = await AppConn.get_openai(
                ctx=ctx, db=self.db, user=self.user, provider=self.action_config.provider
            )
            self.action_config.prompt = f"{self.action_config.prompt}\n{action_input.text}"
            image_req = ImageReq(**self.action_config.model_dump(exclude_none=True))
            images = await openai_client.openai_images.create_image(app_state=app_state, image_req=image_req)

            match images:
                case BadRequestError():
                    raise HTTPException(status_code=images.status_code, detail=images.message)
                case _:
                    data_blocks = DataBlocks()
                    for image in images.data:
                        text = image.revised_prompt
                        url = image.url
                        try:
                            s3 = get_public_s3()
                            user_files = UserFiles(user=self.user, s3=s3)
                            file_metas = await user_files.upload_url(ctx=ctx, file_url=image.url)
                            assert file_metas and len(file_metas) > 0 and file_metas[0].url, (
                                "Error in uploading openai file"
                            )
                            url = str(file_metas[0].url)
                        except Exception as e:
                            logger.bind(ctx=ctx).exception(
                                f"Error in uploading openai file: {str(e)}"
                            )
                        data_block = DataBlock(text=text, urls=[url])
                        data_blocks.blocks.append(data_block)

                    return data_blocks
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in run action: {str(e)}")
            return e

    async def run_tool(
        self, action_config: DalleOpenaiConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        self.action_config = action_config
        action_output = await self.run_action(ctx=ctx, app_state=app_state, action_input=DataBlock())
        return action_output
