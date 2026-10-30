from typing import List, Type

import httpx
from loguru import logger
from pydantic import BaseModel, Field

from src.autobots import SettingsProvider
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
from src.autobots.conn.aws.aws_s3 import get_s3
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

prefix = "test_" + str(gen_uuid())
s3 = get_s3(object_prefix=prefix)


class ConvertConfig(BaseModel):
    url: str = Field(..., description="URL of the file to convert")


class ActionText2TextConvertToPDForText(
    ActionABC[ConvertConfig, ConvertConfig, ConvertConfig, DataBlock, DataBlocks]
):
    type = ActionType.text2text_convert_file_to_pdf_or_txt

    OTHER_FILE_TYPES = ["DOCX", "CSV", "HTML", "JSON", "XLSX"]

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return ConvertConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return ConvertConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return ConvertConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    async def update_config_with_prev_results(
        ctx: Context, app_state: AppState, curr_config: ConvertConfig, prev_results: List[ActionResult] | None = None
    ) -> ConvertConfig:
        return curr_config

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            urls = action_input.urls
            url_api = SettingsProvider.sget().LIBREOFFICE_CONVERSION_API_URL
            token = self.user.access_token
            data_block_urls_list = []
            for url in urls:
                source_url = url
                params = {"url": source_url}
                headers = {"Authorization": f"Bearer {token}"}
                async with httpx.AsyncClient(timeout=300) as client:
                    r = await client.post(url_api, params=params, headers=headers)
                    r.raise_for_status()
                    # output_pdf = Path(r.json() if r.headers.get("content-type","").startswith("application/json") else r.text.strip('"'))
                    converted_url = (
                        r.json()
                        if r.headers.get("content-type", "").startswith("application/json")
                        else r.text.strip('"')
                    )
                    logger.bind(ctx=ctx).info(f"Converted file URL: {converted_url}")
                    data_block_urls_list.append(converted_url)
            data_blocks = DataBlocks(blocks=[DataBlock(urls=data_block_urls_list)])
            return data_blocks
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in converting file: {str(e)}")
            raise e
