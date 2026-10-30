from typing import Optional, Type

from loguru import logger

from src.autobots.action.action.action_doc_model import ActionCreate
from src.autobots.action.action.common_action_models import TextObj
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
from src.autobots.conn.flux.flux_images.image_model import FluxImageReq, FluxImagesResponse
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class ActionCreateGenImageFlux(ActionCreate):
    type: ActionType = ActionType.text2img_flux
    config: FluxImageReq
    input: Optional[TextObj] = None
    output: Optional[FluxImagesResponse] = None


class ActionGenImageFlux(
    ActionABC[FluxImageReq, FluxImageReq, FluxImageReq, TextObj, FluxImagesResponse]
):
    type = ActionType.text2img_flux

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return FluxImageReq

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return FluxImageReq

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return FluxImageReq

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return TextObj

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return FluxImagesResponse

    @staticmethod
    def get_description():
        return """
        Description
        -----------
        Generates images using the Flux image generation API. The configured
        prompt is combined with any additional text provided at runtime and
        submitted to the Flux client. The service produces one or more images
        based on the prompt and model parameters. The raw Flux response is
        returned directly. Any unexpected errors during request construction or
        API execution are captured and returned.

        Input
        -----
        config : dict
            Expected structure:
                {
                    "prompt": "<base prompt for image generation>",
                    "model": "<model identifier like flux-dev , flux-pro>",
                    "num_outputs": <number of images>,
                    "guidance": <guidance scale>,
                    "steps": <inference steps>,
                    "seed": <optional random seed>
                }

            prompt : str
                The main text prompt used to guide image generation. Any runtime
                input text is appended to this base prompt.

            model : str
                Flux model used for image synthesis.eg. flux-dev , flux-pro , flux-schnell

            num_outputs : int
                Number of images to generate.

            guidance : float
                Strength of prompt adherence.

            steps : int
                Inference steps used by the Flux model.

            seed : int | None
                Optional seed controlling determinism.

        Output
        ------
        dict
            Expected structure:
                {
                    "images": [
                        {
                            "url": "<image url or encoded image data>",
                            ...
                        }
                    ]
                }

            Returns the full Flux image generation response, which may contain
            one or more generated images. If the Flux client request fails or an
            error occurs during processing, the exception is returned instead.
        """

    # def __init__(self, action_config: FluxImageReq, user: UserORM | None = None):
    #     super().__init__(action_config=action_config, user=user)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: TextObj, notify: Notify | None = None
    ) -> FluxImagesResponse | Exception:
        try:
            self.action_config.prompt = f"{self.action_config.prompt} {action_input.text}"
            client = await AppConn.get_replicate_flux(ctx=ctx, db=self.db, user=self.user)
            images = await client.create_image(app_state, self.action_config)
            return images
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    async def run_tool(
        self, action_config: FluxImageReq, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> FluxImagesResponse | Exception:
        try:
            client = await AppConn.get_replicate_flux(ctx=ctx, db=self.db, user=self.user)
            images = await client.create_image(app_state, action_config)
            return images
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e
