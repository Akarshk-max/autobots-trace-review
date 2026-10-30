import json
from typing import Dict, List, Literal, Type

from loguru import logger
from openai.types import FileObject
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import ActionABC
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.openai.openai_client import OpenAI
from src.autobots.conn.openai.openai_common_models import (
    OPENAI_FILE_FORMATS,
    OPENAI_FILE_FORMATS_CODE_INTERPRETER,
)
from src.autobots.data_model.context import Context
from src.autobots.files.file_uploader import FileUploader
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class FileUploaderOpenaiConfig(BaseModel):
    """Configuration for file uploader to OpenAI action."""

    provider: Literal["openai", "azure"] = "openai"
    input_urls: List[str] | None = None  # Used for run_tool


class ActionFileUploaderOpenai(
    ActionABC[
        FileUploaderOpenaiConfig,
        FileUploaderOpenaiConfig,
        FileUploaderOpenaiConfig,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.file_uploader_openai

    @staticmethod
    def get_config_create_type() -> Type[FileUploaderOpenaiConfig]:
        return FileUploaderOpenaiConfig

    @staticmethod
    def get_config_update_type() -> Type[FileUploaderOpenaiConfig]:
        return FileUploaderOpenaiConfig

    @staticmethod
    def get_config_type() -> Type[FileUploaderOpenaiConfig]:
        return FileUploaderOpenaiConfig

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
- Upload files from URLs to OpenAI Files API.
- Downloads files from provided URLs and uploads them to OpenAI.
- Returns OpenAI file_id for each uploaded file.

When to use this tool:
- Use when you discover public URLs inside a user-uploaded file and need to upload those linked files to OpenAI so native tools like code interpreter and file search can access them via using file_ids (e.g., a CSV file containing links to other CSVs or documents).
- Use when you need to upload files to OpenAI for use with Assistants or Responses API.
- Use when you have file URLs that need to be converted to OpenAI file_ids.
- Use before calling OpenAI APIs that require file_ids.

Input (DataBlock):
- urls: List of file URLs to upload to OpenAI

Config:
- provider: 'openai' or 'azure' (default: 'openai')

Output (DataBlocks):
- Single DataBlock containing:
  - text: JSON dict mapping URL to FileObject details
  - file_ids: List of OpenAI file_ids for uploaded files
"""

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            if notify:
                notify(ctx, "Starting file upload to OpenAI")

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

            logger.bind(ctx=ctx, urls=action_input.urls).info("Starting file upload to OpenAI")

            if not action_input.urls:
                raise ValueError("urls field is required and must contain at least one URL")

            # Get OpenAI client
            openai_conn: OpenAI | Exception = await AppConn.get_openai(
                ctx=ctx,
                db=self.db,
                user=self.user,
                provider=self.action_config.provider,
            )
            if isinstance(openai_conn, Exception):
                raise openai_conn

            # Upload URLs to OpenAI
            supported_file_formats = OPENAI_FILE_FORMATS + OPENAI_FILE_FORMATS_CODE_INTERPRETER
            url_to_file_object: Dict[str, FileObject] = await FileUploader.upload_urls_to_openai(
                ctx=ctx,
                user=self.user,
                db=self.db,
                urls=action_input.urls,
                openai_client=openai_conn,
                supported_file_formats=supported_file_formats,
            )

            logger.bind(ctx=ctx, uploaded_files=url_to_file_object).info("Files uploaded to OpenAI")

            # Build output: JSON dict mapping URL to FileObject, and list of file_ids
            url_to_file_object_dict: Dict[str, dict] = {}
            file_ids: List[str] = []
            for url, file_object in url_to_file_object.items():
                url_to_file_object_dict[url] = file_object.model_dump()
                file_ids.append(file_object.id)
                # logger.bind(ctx=ctx, url=url, file_id=file_object.id).info(
                #     "File uploaded successfully"
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
                notify(ctx, f"File upload to OpenAI completed. Uploaded {len(file_ids)} file(s)")

            logger.bind(ctx=ctx, num_files=len(file_ids)).info("File upload to OpenAI completed")

            return output
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in file upload to OpenAI: {str(e)}")
            return e

    async def run_tool(
        self, action_config: FileUploaderOpenaiConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlock | Exception:
        try:
            logger.bind(ctx=ctx, action_config=action_config).info(
                "Running file uploader OpenAI tool"
            )
            if not action_config.input_urls:
                raise ValueError("input_urls is required for run_tool")
            # Override instance config with passed config
            self.action_config = action_config
            result = await self.run_action(ctx, app_state, DataBlock(urls=action_config.input_urls), notify)
            if isinstance(result, Exception):
                return result
            # Extract file_ids and text from run_action output
            file_ids: List[str] = []
            text = ""
            for block in result.blocks:
                for fid in block.file_ids:
                    file_ids.append(fid)
                if block.text:
                    text = block.text
            return DataBlock(file_ids=file_ids, text=text)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in file uploader OpenAI tool: {str(e)}")
            return e
