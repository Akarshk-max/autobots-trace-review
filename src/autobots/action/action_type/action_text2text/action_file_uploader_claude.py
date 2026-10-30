import json
from typing import Dict, List, Type

from anthropic.types.beta import BetaFileMetadata
from loguru import logger
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import ActionABC
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.data_model.context import Context
from src.autobots.files.file_uploader import FileUploader
from src.autobots.server_sent_events.server_sent_events import Notify

from src.autobots.data_model.app_state import AppState

class FileUploaderClaudeConfig(BaseModel):
    """Configuration for file uploader to Claude action."""
    input_urls: List[str] | None = None


class ActionFileUploaderClaude(
    ActionABC[
        FileUploaderClaudeConfig,
        FileUploaderClaudeConfig,
        FileUploaderClaudeConfig,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.file_uploader_claude

    @staticmethod
    def get_config_create_type() -> Type[FileUploaderClaudeConfig]:
        return FileUploaderClaudeConfig

    @staticmethod
    def get_config_update_type() -> Type[FileUploaderClaudeConfig]:
        return FileUploaderClaudeConfig

    @staticmethod
    def get_config_type() -> Type[FileUploaderClaudeConfig]:
        return FileUploaderClaudeConfig

    @staticmethod
    def get_input_type() -> Type[DataBlock]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[DataBlocks]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
Description:
- Upload files from URLs to Claude Files API.
- Downloads files from provided URLs and uploads them to Claude.
- Returns Claude file_id for each uploaded file.

When to use this tool:
- Use when you need to upload files to Claude for reuse in Messages API requests.
- Use when you have file URLs that need to be converted to Claude file_ids.
- Use before Claude API calls that should reference uploaded files instead of raw content.

Input (DataBlock):
- urls: List of file URLs to upload to Claude

Output (DataBlocks):
- Single DataBlock containing:
  - text: JSON dict mapping URL to Claude BetaFileMetadata details
  - file_ids: List of Claude file_ids
"""

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        try:
            if notify:
                notify(ctx, "Starting file upload to Claude")

            if not action_input.urls:
                action_input.urls = []
                # logger.bind(ctx=ctx).info("Initialized empty urls list")

            if action_input.metadata.process_url_in_text:
                action_input.process_url_in_text(ctx)
                # logger.bind(ctx=ctx, extracted_urls=action_input.urls).info(
                #     "Extracted URLs from text field"
                # )

            # logger.bind(ctx=ctx, urls=action_input.urls).info("Starting file upload to Claude")

            if not action_input.urls:
                raise ValueError("urls field is required and must contain at least one URL")

            claude_conn = await AppConn.get_claude(
                ctx=ctx,
                db=self.db,
                user=self.user,
            )
            if isinstance(claude_conn, Exception):
                raise claude_conn

            url_to_file_object: Dict[str, BetaFileMetadata] = await FileUploader.upload_urls_to_claude(
                ctx=ctx,
                user=self.user,
                db=self.db,
                urls=action_input.urls,
                claude_client=claude_conn,
                supported_file_formats=None,
            )

            url_to_file_object_dict: Dict[str, dict] = {}
            file_ids: List[str] = []

            for url, file_object in url_to_file_object.items():
                url_to_file_object_dict[url] = file_object.model_dump(mode="json")
                file_ids.append(file_object.id)
                # logger.bind(ctx=ctx, url=url, file_id=file_object.id).info(
                #     "Claude file uploaded successfully"
                # )

            output = DataBlocks(
                blocks=[
                    DataBlock(
                        text=json.dumps(url_to_file_object_dict),
                        file_ids=file_ids,
                    )
                ]
            )

            if notify:
                notify(ctx, f"File upload to Claude completed. Uploaded {len(file_ids)} file(s)")

            logger.bind(ctx=ctx, num_files=len(file_ids)).info("File upload to Claude completed")
            return output

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in file upload to Claude: {str(e)}")
            return e

    async def run_tool(
        self,
        action_config: FileUploaderClaudeConfig,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> DataBlock | Exception:
        try:
            logger.bind(ctx=ctx, action_config=action_config).info(
                "Running file uploader Claude tool"
            )

            if not action_config.input_urls:
                raise ValueError("input_urls is required for run_tool")

            self.action_config = action_config
            result = await self.run_action(
                ctx,
                app_state,
                DataBlock(urls=action_config.input_urls),
                notify,
            )
            if isinstance(result, Exception):
                return result

            file_ids: List[str] = []
            for block in result.blocks:
                for fid in block.file_ids:
                    file_ids.append(fid)

            return DataBlock(file_ids=file_ids)

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in file uploader Claude tool: {str(e)}")
            return e