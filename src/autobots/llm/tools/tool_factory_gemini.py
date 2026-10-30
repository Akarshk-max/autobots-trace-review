import inspect
import json
from io import BytesIO
from typing import Any, List

from google.genai import types
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.gemini.fn_defn import GeminiFunctionDefinitionGen
from src.autobots.core.logging.app_code import AppCode
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.llm.tools.tool_factory import ToolFactory
from src.autobots.llm.tools.tools_map import TOOLS_MAP
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


class ToolFactoryGemini:
    def __init__(self, user: UserORM | None = None, db: AsyncIOMotorDatabase | None = None):
        self.user = user
        self.db = db

    @staticmethod
    async def get_tools() -> List[str]:
        return list(TOOLS_MAP.keys())

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
                logger.bind(code=AppCode.TOOL_FACTORY_GEMINI).exception("Failed getting description for tool {}", tool_name)

        if not description:
            description = inspect.getdoc(callable_obj) or inspect.getdoc(action) or ""

        return callable_obj, description

    @staticmethod
    async def get_tool_definations(tool_names: List[str]) -> List[types.Tool] | None:
        tools: list[types.Tool] = []
        for tool_name in tool_names:
            try:
                action = TOOLS_MAP.get(tool_name)
                if action is None:
                    logger.bind(code=AppCode.TOOL_FACTORY_GEMINI).warning("Tool {} not found in TOOLS_MAP", tool_name)
                    continue

                callable_obj, description = ToolFactoryGemini._get_action_callable_and_description(
                    tool_name=tool_name,
                    action=action,
                )
                func_def = await GeminiFunctionDefinitionGen.get_definition(
                    callable_obj,
                    description,
                )
                func_def.name = tool_name
                if not getattr(func_def, "description", None):
                    func_def.description = description or tool_name
                tools.append(types.Tool(function_declarations=[func_def]))
            except Exception as e:
                logger.bind(code=AppCode.TOOL_FACTORY_GEMINI, tool=tool_name).exception(
                    f"Error in get_tool_definations for tool {tool_name}: {str(e)}"
                )
        return tools if tools else None

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
            and tool_output.text
            and len(tool_output.text) > output_text_to_file_char_limit
        ):
            s3 = get_public_s3()
            user_files = UserFiles(s3=s3, user=user)
            filename = str(gen_uuid()) + ".txt"
            notify(ctx=ctx, message="Tool output too long, saving to file") if notify else None
            file_metas = await user_files.upload_binary(
                ctx=ctx,
                file_obj=BytesIO(tool_output.text.encode("utf-8")),
                filename=filename,
            )
            file_meta = file_metas[0]
            tool_output.text = (
                f"{tool_output.text[:1000]}...\n\nOutput stored in file url: {str(file_meta.url)}"
            )
            if file_meta.url and str(file_meta.url) not in tool_output.urls:
                tool_output.urls.append(str(file_meta.url))
            logger.bind(
                ctx=ctx,
                code=AppCode.TOOL_FACTORY_GEMINI,
                tool_name=tool_name,
                tool_args=repr(tool_args)[:50]
            ).info(f"Converted tool output to file url: {str(file_meta.url)}")
        return tool_output

    @staticmethod
    def _collect_urls_from_payload(payload: Any) -> list[str]:
        urls: list[str] = []
        if isinstance(payload, dict):
            for key in ("url", "s3_url"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    urls.append(value)
            direct_urls = payload.get("urls")
            if isinstance(direct_urls, list):
                for value in direct_urls:
                    if isinstance(value, str) and value:
                        urls.append(value)
            generated_files = payload.get("generated_files")
            if isinstance(generated_files, list):
                for item in generated_files:
                    urls.extend(ToolFactoryGemini._collect_urls_from_payload(item))
            documents = payload.get("documents")
            if isinstance(documents, list):
                for item in documents:
                    urls.extend(ToolFactoryGemini._collect_urls_from_payload(item))
            matches = payload.get("matches")
            if isinstance(matches, list):
                for item in matches:
                    urls.extend(ToolFactoryGemini._collect_urls_from_payload(item))
            payload_field = payload.get("payload")
            if isinstance(payload_field, dict):
                urls.extend(ToolFactoryGemini._collect_urls_from_payload(payload_field))
        elif isinstance(payload, list):
            for item in payload:
                urls.extend(ToolFactoryGemini._collect_urls_from_payload(item))
        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if url not in seen:
                seen.add(url)
                deduped.append(url)
        return deduped

    @staticmethod
    def _collect_file_ids_from_payload(payload: Any) -> list[str]:
        file_ids: list[str] = []
        if isinstance(payload, dict):
            direct = payload.get("file_ids")
            if isinstance(direct, list):
                for item in direct:
                    if isinstance(item, str) and item:
                        file_ids.append(item)
            gemini_file_name = payload.get("gemini_file_name") or payload.get("file_id")
            if isinstance(gemini_file_name, str) and gemini_file_name.startswith("files/"):
                file_ids.append(gemini_file_name)
            for nested_key in ("generated_files", "documents", "matches", "payload"):
                nested = payload.get(nested_key)
                file_ids.extend(ToolFactoryGemini._collect_file_ids_from_payload(nested))
        elif isinstance(payload, list):
            for item in payload:
                file_ids.extend(ToolFactoryGemini._collect_file_ids_from_payload(item))
        deduped: list[str] = []
        seen: set[str] = set()
        for file_id in file_ids:
            if file_id not in seen:
                seen.add(file_id)
                deduped.append(file_id)
        return deduped

    @staticmethod
    def _augment_output_from_serialized_payload(tool_output: DataBlock) -> DataBlock:
        if not tool_output.text:
            return tool_output
        try:
            payload = json.loads(tool_output.text)
        except Exception:
            return tool_output

        for url in ToolFactoryGemini._collect_urls_from_payload(payload):
            if url not in tool_output.urls:
                tool_output.urls.append(url)
        for file_id in ToolFactoryGemini._collect_file_ids_from_payload(payload):
            if file_id not in tool_output.file_ids:
                tool_output.file_ids.append(file_id)
        return tool_output

    async def run_tool(
        self,
        ctx: Context,
        app_state: AppState,
        tool_name,
        tool_args,
        notify: Notify | None = None,
        output_text_to_file_char_limit: int | None = None,
    ) -> DataBlock:
        app_state.raise_if_cancelled()
        tool_factory = ToolFactory(user=self.user, db=self.db)
        tool_output = DataBlock()
        try:
            tool_args_dict = tool_args if isinstance(tool_args, dict) else vars(tool_args)
            if "body" in tool_args_dict and isinstance(tool_args_dict["body"], str):
                try:
                    tool_args_dict["body"] = json.loads(tool_args_dict["body"])
                except json.JSONDecodeError:
                    pass

            notify(ctx, f"Using {tool_name}") if notify else None

            if tool_name == "gemini_context_tool":
                from src.autobots.conn.gemini.context_tool.context_tool_models import (
                    GeminiContextQueryReq,
                )
                from src.autobots.llm.tools.action_context_tool_gemini import (
                    ActionContextToolGemini,
                )

                runtime_turn_history = tool_args_dict.pop("turn_history", None)
                runtime_tool_results = tool_args_dict.pop("tool_results", None)
                runtime_pinned_messages = tool_args_dict.pop("pinned_messages", None)


                context_req = GeminiContextQueryReq.model_validate(tool_args_dict)
                context_req = ActionContextToolGemini.build_request_with_runtime_context(
                    req=context_req,
                    turn_history=runtime_turn_history,
                    tool_results=runtime_tool_results,
                    pinned_messages=runtime_pinned_messages,
                )
                action = ActionContextToolGemini(action_config=context_req, db=self.db, user=self.user)
                context_res = await action.run_tool(context_req, ctx=ctx, app_state=app_state, notify=notify)
                if isinstance(context_res, Exception):
                    raise context_res
                tool_output.text = context_res.model_dump_json(exclude_none=True)

            elif tool_name == "gemini_code_execution_tool":
                tool_output = await tool_factory.run_tool(
                    ctx=ctx,
                    app_state=app_state,
                    tool_name=tool_name,
                    tool_args=tool_args_dict,
                    notify=notify,
                )
                tool_output = ToolFactoryGemini._augment_output_from_serialized_payload(tool_output)
                logger.bind(
                    ctx=ctx,
                    code=AppCode.TOOL_FACTORY_GEMINI,
                    tool_name=tool_name,
                    tool_args=repr(tool_args_dict)[:300],
                    tool_output=repr(tool_output)[:50],
                ).info(f"Ran tool: {tool_name}")
                return tool_output
            else:
                tool_output = await tool_factory.run_tool(
                    ctx=ctx,
                    app_state=app_state,
                    tool_name=tool_name,
                    tool_args=tool_args_dict,
                    notify=notify,
                )
            logger.bind(
                ctx=ctx,
                code=AppCode.TOOL_FACTORY_GEMINI,
                tool_name=tool_name,
                tool_args=repr(tool_args_dict)[:300],
                tool_output=repr(tool_output)[:200],
            ).info(f"Ran tool: {tool_name}")
            tool_output = ToolFactoryGemini._augment_output_from_serialized_payload(tool_output)
            tool_output = await ToolFactoryGemini.maybe_save_output_to_file(
                ctx=ctx,
                user=self.user,
                tool_output=tool_output,
                tool_name=tool_name,
                tool_args=tool_args_dict,
                notify=notify,
                output_text_to_file_char_limit=output_text_to_file_char_limit,
            )
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.TOOL_FACTORY_GEMINI).exception(f"Error in run tool: {str(e)}")
            tool_output.text = str(e)
        return tool_output
