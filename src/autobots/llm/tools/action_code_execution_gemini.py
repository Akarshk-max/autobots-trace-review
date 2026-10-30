import json
from typing import Type

from loguru import logger

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
from src.autobots.conn.gemini.code_execution.code_execution_artifacts import (
    GeminiCodeExecutionArtifacts,
)
from src.autobots.conn.gemini.code_execution.code_execution_client import (
    GeminiCodeExecutionClient,
)
from src.autobots.conn.gemini.code_execution.code_execution_models import (
    GeminiCodeExecutionReq,
    GeminiCodeExecutionRes,
)
from src.autobots.conn.gemini.code_execution.code_execution_parser import (
    GeminiCodeExecutionParser,
)
from src.autobots.conn.gemini.model_registry_gemini import GeminiModelRegistry
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState


class ActionCodeExecutionGemini(
    ActionABC[
        GeminiCodeExecutionReq,
        GeminiCodeExecutionReq,
        GeminiCodeExecutionReq,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.action_text2text_code_execution_gemini

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return GeminiCodeExecutionReq

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return GeminiCodeExecutionReq

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return GeminiCodeExecutionReq

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @classmethod
    def _strip_inline_artifact_data(cls, payload):
        if isinstance(payload, list):
            return [cls._strip_inline_artifact_data(item) for item in payload]
        if not isinstance(payload, dict):
            return payload

        cleaned = {}
        for key, value in payload.items():
            if key in {"inline_data", "inlineData"} and isinstance(value, dict):
                cleaned[key] = {
                    nested_key: cls._strip_inline_artifact_data(nested_value)
                    for nested_key, nested_value in value.items()
                    if nested_key != "data"
                }
                continue
            cleaned[key] = cls._strip_inline_artifact_data(value)
        return cleaned

    @staticmethod
    def get_description() -> str:
        return """
Gemini code execution tool.

This tool uses Gemini with only the Code Execution built-in enabled.
Use it when you need:
- calculations
- CSV / text file analysis
- chart generation
- table/document transformation through Python code
- reading attached files and generating new output files

Best practices
--------------
- Prefer this tool for data processing, charts, and deterministic file analysis.
- Pass files when you want Gemini code execution to inspect them directly.
- Generated output files are persisted to S3 and returned with URLs.

Input
-----
{
  "task": "Describe what to do",
  "files": [
    {"url": "https://.../sales.csv", "display_name": "sales.csv"}
  ],
  "model": "gemini-2.5-pro"
}

Output
------
- summary text
- final response text
- prepared input file manifest
- executed code blocks
- execution outputs
- generated file URLs persisted to S3
"""

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        try:
            result = await self.run_tool(self.action_config, ctx=ctx, app_state=app_state, notify=notify)
            if isinstance(result, Exception):
                return result
            return DataBlocks(
                blocks=[
                    DataBlock(
                        text=json.dumps(result.model_dump(exclude_none=True), indent=2, ensure_ascii=False),
                        urls=GeminiCodeExecutionArtifacts.artifacts_to_datablock_urls(result.generated_files),
                    )
                ]
            )
        except Exception as exc:
            logger.bind(ctx=ctx).exception("Error in ActionCodeExecutionGemini.run_action: {}", exc)
            return exc

    async def run_tool(
        self,
        action_config: GeminiCodeExecutionReq,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> GeminiCodeExecutionRes | Exception:
        try:
            gemini_conn = await AppConn.get_gemini(ctx=ctx, db=self.db, user=self.user)
            if isinstance(gemini_conn, Exception):
                raise gemini_conn

            normalized_req = action_config.model_copy(
                update={"model": action_config.model or GeminiModelRegistry.default_model()}
            )
            client = GeminiCodeExecutionClient(gemini_client=gemini_conn)
            response, prepared_input_files = await client.execute(
                ctx=ctx,
                user=self.user,
                db=self.db,
                req=normalized_req,
            )
            parsed = GeminiCodeExecutionParser.parse_response(response)
            parsed.input_files = [item.model_dump(exclude_none=True) for item in prepared_input_files]

            artifact_parts = (parsed.meta or {}).get("artifact_parts") or []
            if (
                normalized_req.output_mode.capture_generated_files
                and isinstance(artifact_parts, list)
                and artifact_parts
            ):
                artifacts = await GeminiCodeExecutionArtifacts.persist_inline_artifacts(
                    ctx=ctx,
                    user=self.user,
                    parts=artifact_parts,
                )
                parsed.generated_files = [
                    artifact.model_dump(exclude_none=True) for artifact in artifacts
                ]

            sanitized_meta = self._strip_inline_artifact_data(dict(parsed.meta or {}))
            parsed.meta = {
                **sanitized_meta,
                "input_files_count": len(parsed.input_files),
                "generated_files_count": len(parsed.generated_files),
            }
            return parsed
        except Exception as exc:
            logger.bind(ctx=ctx).exception("Error in ActionCodeExecutionGemini.run_tool: {}", exc)
            return exc
