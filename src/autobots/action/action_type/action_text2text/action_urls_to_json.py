import io
import json
from datetime import UTC, datetime
from typing import Type

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import ActionABC
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.core.url_utils import extract_urls
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.files.user_files_model import FileMeta
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


class URLsToJSONConfig(BaseModel):
    urls: list[str] = Field(default_factory=list)
    text: str = ""
    filename_prefix: str = "datablock_urls"


class ActionURLsToJSON(
    ActionABC[URLsToJSONConfig, URLsToJSONConfig, URLsToJSONConfig, DataBlock, DataBlocks]
):
    type = ActionType.urls_to_json

    @staticmethod
    def get_config_create_type() -> Type[URLsToJSONConfig]:
        return URLsToJSONConfig

    @staticmethod
    def get_config_update_type() -> Type[URLsToJSONConfig]:
        return URLsToJSONConfig

    @staticmethod
    def get_config_type() -> Type[URLsToJSONConfig]:
        return URLsToJSONConfig

    @staticmethod
    def get_input_type() -> Type[DataBlock]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[DataBlocks]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
            Description
            -----------
            Takes every URL in the input datablock, writes them into a JSON file as
            {"urls": [...]}, uploads the generated JSON to public S3, and returns
            the uploaded file URL.

            Input
            -----
            action_input : DataBlock
                Uses `urls` directly from the datablock. If `metadata.process_url_in_text`
                is enabled, HTTP/HTTPS URLs are also extracted from the datablock text.

            Config
            ------
            {
                "urls": ["https://example.com/a", "https://example.com/b"],
                "text": "Optional text containing URLs",
                "filename_prefix": "datablock_urls"
            }

            Output
            ------
            DataBlocks with the uploaded JSON URL in both `text` and `urls`.
        """

    @staticmethod
    def _collect_urls(config: URLsToJSONConfig) -> list[str]:
        urls = list(config.urls)
        if config.text:
            extracted = extract_urls(
                config.text,
                allowed_schemes={"http", "https"},
                unique=True,
                validate=True,
            )
            for url in extracted:
                if url not in urls:
                    urls.append(url)
        return urls

    @staticmethod
    async def update_config(
        ctx: Context,
        app_state: AppState,
        config: URLsToJSONConfig,
        config_update: URLsToJSONConfig,
        user: UserORM,
        db: AsyncIOMotorDatabase,
    ) -> URLsToJSONConfig:
        urls = ActionURLsToJSON._collect_urls(config or URLsToJSONConfig())
        for url in ActionURLsToJSON._collect_urls(config_update or URLsToJSONConfig()):
            if url not in urls:
                urls.append(url)

        filename_prefix = config_update.filename_prefix or config.filename_prefix

        return URLsToJSONConfig(
            urls=urls,
            filename_prefix=filename_prefix,
        )

    async def _upload_urls_json(
        self,
        ctx: Context,
        urls: list[str],
        filename_prefix: str,
    ) -> FileMeta:
        if not urls:
            raise HTTPException(
                status_code=400,
                detail="No URLs found in the datablock. Pass URLs in `urls`, or enable URL extraction from text.",
            )

        content = json.dumps({"urls": urls}, indent=2)

        filename = (
            f"{(filename_prefix or 'datablock_urls').strip() or 'datablock_urls'}"
            f"_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
        )

        user_files = UserFiles(user=self.user, s3=get_public_s3())
        file_metas = await user_files.upload_binary(
            ctx=ctx,
            file_obj=io.BytesIO(content.encode("utf-8")),
            filename=filename,
        )
        return file_metas[0]

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            if action_input.metadata.process_url_in_text:
                action_input.process_url_in_text(ctx)

            input_config = URLsToJSONConfig(
                urls=action_input.urls,
                text=action_input.text or "",
            )
            config = await ActionURLsToJSON.update_config(
                ctx=ctx,
                app_state=app_state,
                config=self.action_config,
                config_update=input_config,
                user=self.user,
                db=self.db,
            )
            urls = self._collect_urls(config)
            file_meta = await self._upload_urls_json(
                ctx=ctx,
                urls=urls,
                filename_prefix=config.filename_prefix,
            )

            output = DataBlocks(
                blocks=[
                    DataBlock(
                        text=str(file_meta.url),
                        urls=[str(file_meta.url)],
                    )
                ]
            )
            logger.bind(ctx=ctx, output_url=file_meta.url).info(
                f"Ran action: {ActionURLsToJSON.__name__}"
            )
            return output
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in {ActionURLsToJSON.__name__}: {str(e)}")
            return e

    async def run_tool(
        self, action_config: URLsToJSONConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlock | Exception:
        try:
            config = await self.create_config(
                ctx=ctx,
                app_state=app_state,
                config_create=action_config,
                user=self.user,
                db=self.db,
            )
            urls = self._collect_urls(config)
            file_meta = await self._upload_urls_json(
                ctx=ctx,
                urls=urls,
                filename_prefix=config.filename_prefix,
            )
            output = DataBlock(text=str(file_meta.url), urls=[str(file_meta.url)])
            return output
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in run tool: {str(e)}")
            return e
