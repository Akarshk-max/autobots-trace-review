import csv
import io
import json
from typing import Any, Dict, Literal, Type

from loguru import logger
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import ActionABC
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.core.settings import SettingsProvider
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class JsonFlattenConfig(BaseModel):
    """Configuration for JSON flatten action."""

    separator: str = "|"
    output_type: Literal["json", "csv"] = "json"
    output_format: Literal["text", "file"] = "text"
    json_string: str | None = None  # Used for run_tool


class ActionJsonFlatten(
    ActionABC[JsonFlattenConfig, JsonFlattenConfig, JsonFlattenConfig, DataBlock, DataBlocks]
):
    type = ActionType.json_flatten

    @staticmethod
    def _flatten_helper(
        nested_dict: Dict[str, Any], parent_key: str = "", separator: str = "|"
    ) -> Dict[str, Any]:
        """
        Helper function to recursively flatten a dictionary.
        Returns a dict (not JSON string).
        """
        items = []

        for key, value in nested_dict.items():
            new_key = f"{parent_key}{separator}{key}" if parent_key else key

            if isinstance(value, dict):
                # Recurse and get dict
                items.extend(ActionJsonFlatten._flatten_helper(value, new_key, separator).items())
            else:
                items.append((new_key, value))

        return dict(items)

    @staticmethod
    def flatten_json(
        json_string: str, parent_key: str = "", separator: str = "|"
    ) -> Dict[str, Any]:
        """
        Flatten a nested JSON string and return as flattened dict.

        Args:
            json_string: The nested JSON string to flatten
            parent_key: The base key path (used in recursion)
            separator: The separator to use (default: '|')

        Returns:
            A dict with flattened, pipe-delimited keys
        """
        # Parse JSON string to dict
        nested_dict = json.loads(json_string)

        # Flatten the dict
        flattened = ActionJsonFlatten._flatten_helper(nested_dict, parent_key, separator)

        return flattened

    @staticmethod
    def dict_to_csv(flattened_dict: Dict[str, Any]) -> str:
        """
        Convert a flattened dict to CSV string.
        Keys become headers, values become a single row.
        Arrays/objects in values are serialized as JSON strings.

        Args:
            flattened_dict: The flattened dictionary

        Returns:
            A CSV string with headers and one row of data
        """
        output = io.StringIO()
        writer = csv.writer(output)

        # Write headers (keys)
        headers = list(flattened_dict.keys())
        writer.writerow(headers)

        # Write values (serialize complex types as JSON)
        values = []
        for value in flattened_dict.values():
            if isinstance(value, (dict, list)):
                values.append(json.dumps(value))
            else:
                values.append(value)
        writer.writerow(values)

        return output.getvalue()

    @staticmethod
    def get_config_create_type() -> Type[JsonFlattenConfig]:
        return JsonFlattenConfig

    @staticmethod
    def get_config_update_type() -> Type[JsonFlattenConfig]:
        return JsonFlattenConfig

    @staticmethod
    def get_config_type() -> Type[JsonFlattenConfig]:
        return JsonFlattenConfig

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
- Flatten a nested JSON object into a single-level JSON with pipe-delimited keys.
- Nested keys are joined using '|' separator (e.g., {"a": {"b": 5}} becomes {"a|b": 5}).
- Arrays are preserved as-is and not flattened.
- Supports output as JSON or CSV format.
- Supports output as text or file (uploaded to S3).

When to use this tool:
- Use when you need to convert deeply nested JSON into a flat structure.
- Use when preparing JSON data for systems that don't support nested objects.
- Use when you need to simplify JSON for easier processing or display.
- Use CSV output when you need tabular data format.
- Use file output when you need a downloadable file URL.

Input (DataBlock):
- text: The nested JSON string to flatten

Config:
- separator: The separator to use for flattened keys (default: '|')
- output_type: 'json' or 'csv' (default: 'json')
- output_format: 'text' or 'file' (default: 'text')

Output (DataBlocks):
- If output_format='text': blocks[0].text contains the flattened JSON/CSV string
- If output_format='file': blocks[0].urls contains the S3 file URL

Examples:
- Input: '{"a": {"b": 5}}' → JSON Output: '{"a|b": 5}'
- Input: '{"a": {"b": 5}}' → CSV Output: 'a|b\n5'
- Input: '{"a": {"b": 5, "c": 10}, "d": {"e": 20}}' → JSON Output: '{"a|b": 5, "a|c": 10, "d|e": 20}'
"""

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            if notify:
                notify(ctx, "Starting JSON flatten")

            logger.bind(ctx=ctx, json_string=repr(action_input.text)[:20]).info("Starting JSON flatten")

            if not action_input.text:
                raise ValueError("text field is required and must contain a JSON string")

            separator = self.action_config.separator
            output_type = self.action_config.output_type
            output_format = self.action_config.output_format

            # Flatten the JSON
            flattened_dict = ActionJsonFlatten.flatten_json(action_input.text, separator=separator)

            # Convert to output type (JSON or CSV)
            if output_type == "csv":
                result_string = ActionJsonFlatten.dict_to_csv(flattened_dict)
                file_extension = "csv"
            else:
                result_string = json.dumps(flattened_dict)
                file_extension = "json"

            logger.bind(ctx=ctx, output_type=output_type, output_format=output_format).info(
                "JSON flatten conversion completed"
            )

            # Handle output format (text or file)
            if output_format == "file":
                # Upload to S3 public bucket
                settings = SettingsProvider.sget()
                user_files = UserFiles(user=self.user, s3=get_public_s3(), settings=settings)

                # Convert string to bytes, then to BytesIO
                file_obj = io.BytesIO(result_string.encode("utf-8"))

                # Generate UUID for filename
                uuid = str(gen_uuid())
                filename = f"{uuid}.{file_extension}"

                logger.bind(ctx=ctx, filename=filename).info("Uploading flattened output to S3")

                # Upload using upload_binary method
                file_metas = await user_files.upload_binary(
                    ctx=ctx, file_obj=file_obj, filename=filename
                )

                # Collect URLs from file_metas
                urls = []
                for file_meta in file_metas:
                    urls.append(str(file_meta.url))

                output = DataBlocks(blocks=[DataBlock(urls=urls)])

                logger.bind(ctx=ctx, urls=urls).info("JSON flatten file upload completed")
            else:
                # Return as text
                output = DataBlocks(blocks=[DataBlock(text=result_string)])

                logger.bind(ctx=ctx, result_length=len(result_string)).info(
                    "JSON flatten completed"
                )

            if notify:
                notify(ctx, "JSON flatten completed")

            return output
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in JSON flatten: {str(e)}")
            return e

    async def run_tool(
        self, action_config: JsonFlattenConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            logger.bind(ctx=ctx, action_config=action_config).info("Running JSON flatten tool")
            # For tool usage, input comes via action_config.json_string
            if not action_config.json_string:
                raise ValueError("json_string is required for run_tool")
            # Use the passed action_config for output_type and output_format
            self.action_config = action_config
            return await self.run_action(ctx, app_state, DataBlock(text=action_config.json_string), notify)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in JSON flatten tool: {str(e)}")
            return e
