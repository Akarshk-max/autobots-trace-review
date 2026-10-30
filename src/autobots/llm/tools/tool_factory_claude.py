import inspect
import json
from io import BytesIO
from typing import Any, Dict, List

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.claude.fn_defn import AnthropicFunctionDefinitionGen
from src.autobots.core.logging.app_code import AppCode
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.llm.tools.tool_factory import ToolFactory
from src.autobots.llm.tools.tools_map import TOOLS_MAP
from src.autobots.mcp.mcp_doc_model import MCP_START_CONSTANT
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


class ToolFactoryClaude:
    def __init__(self, user: UserORM | None = None, db: AsyncIOMotorDatabase | None = None):
        self.user = user
        self.db = db

    @staticmethod
    async def get_tools() -> List[str]:
        tools = []
        for tool in TOOLS_MAP.keys():
            tools += [tool]
        return tools

    @staticmethod
    def _get_action_callable_and_description(
        tool_name: str,
        action: Any,
    ) -> tuple[Any, str]:
        if inspect.isfunction(action) or inspect.ismethod(action):
            description = inspect.getdoc(action) or ""
            return action, description

        callable_obj = getattr(action, "run_tool", None)
        if callable_obj is None:
            raise ValueError(f"Tool {tool_name} has no callable run_tool")

        description = ""
        if hasattr(action, "get_description") and callable(action.get_description):
            try:
                description = action.get_description() or ""
            except Exception:
                logger.bind(code=AppCode.TOOL_FACTORY_CLAUDE).exception("Failed getting description for tool {}", tool_name)

        if not description:
            description = inspect.getdoc(callable_obj) or inspect.getdoc(action) or ""

        return callable_obj, description

    @staticmethod
    async def get_tool_definations(tool_names: List[str]) -> List[Dict[str, Any]] | None:
        func_defs: list[dict[str, Any]] = []

        for tool_name in tool_names:
            try:
                if isinstance(tool_name, str) and tool_name.startswith(MCP_START_CONSTANT):
                    func_defs.append({"name": tool_name})
                    continue

                action = TOOLS_MAP.get(tool_name)
                if action is None:
                    logger.bind(code=AppCode.TOOL_FACTORY_CLAUDE).warning("Tool {} not found in TOOLS_MAP", tool_name)
                    continue

                callable_obj, description = ToolFactoryClaude._get_action_callable_and_description(
                    tool_name=tool_name,
                    action=action,
                )

                func_def = await AnthropicFunctionDefinitionGen.get_definition(
                    callable_obj,
                    description,
                )

                # Always bind the public tool name to the map key, not the Python function name.
                func_def["name"] = tool_name

                # Ensure description is never empty/falsy
                func_def["description"] = description or tool_name

                # AnthropicFunctionDefinitionGen already returns a finalized Claude-safe schema.
                input_schema = func_def.get("input_schema")
                if not isinstance(input_schema, dict):
                    func_def["input_schema"] = {"type": "object", "properties": {}}
                else:
                    func_def["input_schema"] = input_schema

                func_defs.append(func_def)

            except Exception as e:
                logger.bind(code=AppCode.TOOL_FACTORY_CLAUDE).exception("Error in get_tool_definitions for {}: {}", tool_name, str(e))

        return func_defs if func_defs else None

    @staticmethod
    async def maybe_save_output_to_file(
        ctx: Context,
        user: UserORM,
        tool_output: DataBlock,
        tool_name: str,
        tool_args,
        notify: Notify | None = None,
        output_text_to_file_char_limit: int | None = 50000,
    ) -> DataBlock:
        if (
            output_text_to_file_char_limit
            and len(tool_output.text) > output_text_to_file_char_limit
        ):
            s3 = get_public_s3()
            user_files = UserFiles(s3=s3, user=user)
            filename = str(gen_uuid()) + ".txt"
            notify(ctx=ctx, message="Tool output too long, saving to file") if notify else None
            file_metas = await user_files.upload_binary(
                ctx=ctx, file_obj=BytesIO(tool_output.text.encode("utf-8")), filename=filename
            )
            file_meta = file_metas[0]
            tool_output.text = (
                f"{tool_output.text[:1000]}...\n\nOutput stored in file url: {str(file_meta.url)}"
            )
            logger.bind(
                ctx=ctx, code=AppCode.TOOL_FACTORY_CLAUDE, tool_name=tool_name, tool_args=repr(tool_args)[:50]
            ).info(f"Converted tool output to file url: {str(file_meta.url)}")
            return DataBlock(
                text=tool_output.text,
                urls=[str(file_meta.url)] if file_meta.url else [],
                file_ids=tool_output.file_ids,
            )
        else:
            return tool_output

    async def run_tool(
        self,
        ctx: Context,
        app_state: AppState,
        tool_name,
        tool_args,
        notify: Notify | None = None,
        output_text_to_file_char_limit: int | None = 50000,
    ) -> DataBlock:
        app_state.raise_if_cancelled()
        tool_factory = ToolFactory(user=self.user, db=self.db)
        tool_output = DataBlock()
        try:
            # Convert tool_args to dictionary if it's not already
            tool_args_dict = tool_args if isinstance(tool_args, dict) else vars(tool_args)
            if "body" in tool_args_dict:
                if isinstance(tool_args_dict["body"], str):
                    try:
                        tool_args_dict["body"] = json.loads(tool_args_dict["body"])
                    except json.JSONDecodeError:
                        # Keep as string if not valid JSON
                        pass

            notify(ctx, f"Using {tool_name}") if notify else None
            tool_output: DataBlock = await tool_factory.run_tool(
                ctx=ctx, app_state=app_state, tool_name=tool_name, tool_args=tool_args_dict, notify=notify
            )
            logger.bind(
                ctx=ctx, code=AppCode.TOOL_FACTORY_CLAUDE, tool_name=tool_name, tool_args=repr(tool_args_dict)[:300], tool_output=repr(tool_output)[:200]
            ).info(f"Ran tool: {tool_name}")
            tool_output = await ToolFactoryClaude.maybe_save_output_to_file(
                ctx=ctx,
                user=self.user,
                tool_output=tool_output,
                tool_name=tool_name,
                tool_args=tool_args,
                notify=notify,
                output_text_to_file_char_limit=output_text_to_file_char_limit,
            )

        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.TOOL_FACTORY_CLAUDE).exception(f"Error in run tool: {str(e)}")
            tool_output.text = str(e)

        return tool_output
