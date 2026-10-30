import json
from typing import Dict, List, Type

from google.genai import types
from loguru import logger
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import ActionABC
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.gemini.gemini import GeminiClient
from src.autobots.data_model.context import Context
from src.autobots.files.file_uploader import FileUploader
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState


class FileUploaderGeminiConfig(BaseModel):
    """Configuration for file uploader to Gemini action."""

    input_urls: List[str] | None = None  # Used for run_tool


class ActionFileUploaderGemini(
    ActionABC[
        FileUploaderGeminiConfig,
        FileUploaderGeminiConfig,
        FileUploaderGeminiConfig,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.file_uploader_gemini

    @staticmethod
    def get_config_create_type() -> Type[FileUploaderGeminiConfig]:
        return FileUploaderGeminiConfig

    @staticmethod
    def get_config_update_type() -> Type[FileUploaderGeminiConfig]:
        return FileUploaderGeminiConfig

    @staticmethod
    def get_config_type() -> Type[FileUploaderGeminiConfig]:
        return FileUploaderGeminiConfig

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
- Upload files from URLs to Gemini Files API.
- Downloads files from provided URLs and uploads them to Gemini.
- Returns Gemini file_id (file.name) for each uploaded file.

When to use this tool:
- Use when you need to upload files to Gemini for use with code execution.
- Use when you have file URLs that need to be converted to Gemini file_ids.
- Use before calling Gemini APIs that require file_ids.

Input (DataBlock):
- urls: List of file URLs to upload to Gemini

Output (DataBlocks):
- Single DataBlock containing:
  - text: JSON dict mapping URL to file details (name, uri)
  - file_ids: List of Gemini file names for uploaded files
"""

    # https://ai.google.dev/gemini-api/docs/files#usage_info - file size limitations and for how long it is stored official docs - 48h is the time of storage
    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            if notify:
                notify(ctx, "Starting file upload to Gemini")

            # Initialize urls if None
            if not action_input.urls:
                action_input.urls = []
                # logger.bind(ctx=ctx).info("Initialized empty urls list")

            # Extract URLs from text if process_url_in_text is enabled
            if action_input.metadata.process_url_in_text:
                action_input.process_url_in_text(ctx)
                # logger.bind(ctx=ctx, extracted_urls=action_input.urls).info(
                #     "Extracted URLs from text field"
                # )

            logger.bind(ctx=ctx, urls=action_input.urls).info("Starting file upload to Gemini")

            if not action_input.urls:
                raise ValueError("urls field is required and must contain at least one URL")

            # Get Gemini client
            gemini_conn: GeminiClient | Exception = await AppConn.get_gemini(
                ctx=ctx,
                db=self.db,
                user=self.user,
            )
            if isinstance(gemini_conn, Exception):
                raise gemini_conn

            # Upload URLs to Gemini
            url_to_file: Dict[str, types.File] = await FileUploader.upload_urls_to_gemini(
                ctx=ctx,
                user=self.user,
                db=self.db,
                urls=action_input.urls,
                gemini_client=gemini_conn,
            )

            # Extract file IDs (file.name is the Gemini file ID)
            file_ids: List[str] = []
            for file in url_to_file.values():
                file_ids.append(file.name)

            # Create JSON mapping for text output
            url_to_file_map = {}
            for url, file in url_to_file.items():
                url_to_file_map[url] = {
                    "name": file.name,
                    "uri": file.uri,
                }

            # Create output DataBlock
            output = DataBlocks(
                blocks=[
                    DataBlock(
                        text=json.dumps(url_to_file_map, indent=2),
                        file_ids=file_ids,
                    )
                ]
            )

            if notify:
                notify(ctx, f"File upload to Gemini completed. Uploaded {len(file_ids)} file(s)")

            logger.bind(ctx=ctx, num_files=len(file_ids)).info("File upload to Gemini completed")

            return output
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in file upload to Gemini: {str(e)}")
            return e

    async def run_tool(
        self,
        action_config: FileUploaderGeminiConfig,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> DataBlock | Exception:
        try:
            logger.bind(ctx=ctx, action_config=action_config).info(
                "Running file uploader Gemini tool"
            )

            if not action_config.input_urls:
                raise ValueError("input_urls is required for run_tool")

            # Override instance config with passed config
            self.action_config = action_config

            # Build DataBlock safely
            action_input = DataBlock(urls=action_config.input_urls)

            if getattr(action_input, "metadata", None) is None:

                class _FallbackMeta(BaseModel):
                    process_url_in_text: bool = False

                action_input.metadata = _FallbackMeta()

            result = await self.run_action(ctx, app_state, action_input, notify)

            if isinstance(result, Exception):
                return result

            # Extract file_ids safely
            file_ids: List[str] = []
            for block in result.blocks or []:
                for fid in block.file_ids or []:
                    if fid:
                        file_ids.append(fid)

            return DataBlock(file_ids=file_ids)

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in file uploader Gemini tool: {str(e)}")
            return e
