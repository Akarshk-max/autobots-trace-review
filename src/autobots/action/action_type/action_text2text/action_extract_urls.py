from typing import Type

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import ActionABC
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

class ExtractURLsConfig(DataBlock):
    pass


class ActionExtractURLs(
    ActionABC[ExtractURLsConfig, ExtractURLsConfig, ExtractURLsConfig, DataBlock, DataBlocks]
):
    type = ActionType.extract_urls

    @staticmethod
    def get_config_create_type() -> Type[ExtractURLsConfig]:
        return ExtractURLsConfig

    @staticmethod
    def get_config_update_type() -> Type[ExtractURLsConfig]:
        return ExtractURLsConfig

    @staticmethod
    def get_config_type() -> Type[ExtractURLsConfig]:
        return ExtractURLsConfig

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
            Extracts all HTTP/HTTPS URLs found either in the supplied text or in the `urls` field, normalises & de-duplicates them

            Input
            -----
            config : dict
                A dictionary of arguments supplied by the caller.
                Expected structure:
                    {
                        "text": "Some text containing URLs: https://example.com/sample.html",
                        "urls": [
                            "https://example.com/a.csv",
                            "https://example.com/b.csv",
                            ...
                        ]
                    }

                text : str
                    A string containing text to be processed for URLs.
                urls : list[str]
                    A list of CSV file URLs. Each URL should point directly to a valid
                    CSV file. The function downloads each CSV in the order provided and
                    merges them vertically.

            Output
            ------
            {
                "text": "Some text containing URLs: https://example.com/sample.html",
                "urls": [
                    "https://example.com/a.csv",
                    "https://example.com/b.csv",
                    "https://example.com/sample.html"
                ]
            }
        """

    @staticmethod
    async def update_config(
        ctx: Context,
        app_state: AppState,
        config: ExtractURLsConfig,
        config_update: ExtractURLsConfig,
        user: UserORM,
        db: AsyncIOMotorDatabase,
    ) -> ExtractURLsConfig:
        updated_config = ExtractURLsConfig()
        updated_config.text = ""
        if config and config.text:
            updated_config.text += config.text
        if config_update and config_update.text:
            updated_config.text += config_update.text
        updated_config.urls = []
        if config and config.urls:
            updated_config.urls += config.urls
        if config_update and config_update.urls:
            updated_config.urls += config_update.urls
        return updated_config

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            config_update = ExtractURLsConfig.model_validate(action_input)
            config = await ActionExtractURLs.update_config(
                ctx, app_state, self.action_config, config_update, user=self.user, db=self.db
            )
            config.process_url_in_text(ctx=ctx)
            config.text = ""
            datablocks = DataBlocks(blocks=[config])
            return datablocks
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in {ActionExtractURLs.__name__}: {str(e)}")
            return e

    async def run_tool(
        self, action_config: ExtractURLsConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlock | Exception:
        try:
            config = await self.create_config(
                ctx=ctx, app_state=app_state, config_create=action_config, user=self.user, db=self.db
            )
            config.process_url_in_text(ctx=ctx)
            config.text = ""
            return config
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in run tool: {str(e)}")
            return e
