import io
from typing import List

import replicate.client
from fastapi import UploadFile
from loguru import logger
from replicate.model import Model

from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.flux.flux_images.image_model import FluxImageReq, FluxImagesResponse
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.files.user_files_model import FileMeta
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

class FluxClient:
    def __init__(
        self,
        ctx: Context,
        user: UserORM,
        api_key: str,
        notify: Notify = None,
        base_url: str = "https://api.replicate.com/v1/models/black-forest-labs/flux-pro",
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.client = replicate.client.Client(api_token=api_key)
        self.ctx = ctx
        self.user = user
        self.default_seed = 0

    async def create_image(self, app_state: AppState, image_req: FluxImageReq) -> FluxImagesResponse:
        model: Model = self.client.models.get(key="black-forest-labs/" + image_req.model_name)
        ref = f"{model.id}:{model.latest_version.id}"
        output_dict = await self.client.async_run(
            ref, image_req.model_dump(exclude={"model_name"}, exclude_none=True)
        )

        aws_s3 = get_public_s3(object_prefix="save-to-s3-tool")
        user_files = UserFiles(user=self.user, s3=aws_s3)
        files: List[UploadFile] = []

        for idx, output in enumerate(output_dict):
            image_bytes = output.read()
            binary = io.BytesIO(image_bytes)
            upload_file = UploadFile(filename=f"replicate_{idx}.png", file=binary)
            files.append(upload_file)
        logger.bind(ctx=self.ctx).info(f"files are {files}")
        try:
            file_metas: List[FileMeta] = await user_files.upload_files(ctx=self.ctx, files=files)
            logger.bind(ctx=self.ctx).info(f"Successfully uploaded {len(file_metas)} files to S3")
        except Exception as e:
            logger.bind(ctx=self.ctx).error(f"Failed to upload images to S3: {e}")
            raise RuntimeError(f"S3 upload failed: {e}") from e

        file_metas_dicts = [{"name": fm.name, "url": str(fm.url)} for fm in file_metas]
        seed = image_req.seed if image_req.seed is not None else self.default_seed
        # Process the Replicate API response to match FluxImagesResponse format
        images = [{"url": fm["url"], "seed": seed} for fm in file_metas_dicts]
        return FluxImagesResponse(images=images, prompt=image_req.prompt)


def get_flux_client(
    ctx: Context,
    user: UserORM,
    api_key: str,
    notify: Notify = None,
    settings: Settings = SettingsProvider.sget(),
) -> FluxClient:
    _flux_client = FluxClient(ctx=ctx, user=user, api_key=api_key, notify=notify)
    return _flux_client
