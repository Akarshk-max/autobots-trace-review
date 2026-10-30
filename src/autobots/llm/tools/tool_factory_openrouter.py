import asyncio
import inspect
import json
from io import BytesIO
from typing import Any, List

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai.types.chat import ChatCompletionToolParam

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.openrouter.openrouter_function_definition_gen import (
    OpenRouterFunctionDefinitionGen,
)
from src.autobots.core.logging.app_code import AppCode
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.llm.tools.tool_factory import ToolFactory
from src.autobots.llm.tools.tools_map import TOOLS_MAP
from src.autobots.mcp.mcp_client import MCPClientWrapper, MCPToolBinding
from src.autobots.mcp.mcp_doc_model import MCPDoc
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


class ToolFactoryOpenRouter:

    # offloading to file url rules
    NO_OFFLOAD_VECTOR_STORE_OPS: set[str] = {
        "create_vector_store",
        "add_files",
        "reindex_files",
        "delete_vector_store_files",
        "delete_vector_store",
    }

    OFFLOAD_TOOL_INPUT_ALLOWLIST: set[str] = {"daytona_sandbox", "file_system_tool"}

    def __init__(self, user: UserORM | None = None, db: AsyncIOMotorDatabase | None = None):
        self.user = user
        self.db = db

    @staticmethod
    async def get_chat_completion_tool_param(
        ctx: Context,
        tool_names: List[str],
        schema_profile: str = "default",
    ) -> List[ChatCompletionToolParam] | None:
        func_defs: list[ChatCompletionToolParam] = []

        tool_def_builder = (
            OpenRouterFunctionDefinitionGen.get_chat_completion_tool_param_no_refs
            if str(schema_profile).strip().lower() == "no_refs"
            else OpenRouterFunctionDefinitionGen.get_chat_completion_tool_param
        )

        for tool_name in tool_names:
            # logger.bind(ctx=ctx, tool_name=tool_name, code=AppCode.TOOL_FACTORY_OPENROUTER).info(
            #     "Starting get_chat_completion_tool_param (OpenRouter) for tool"
            # )
            try:
                action = TOOLS_MAP.get(tool_name)
                if not action:
                    logger.bind(ctx=ctx, tool_name=tool_name, code=AppCode.TOOL_FACTORY_OPENROUTER).warning(
                        "Tool not found in TOOLS_MAP; skipping"
                    )
                    continue
                if inspect.isfunction(action):
                    func_def = await tool_def_builder(action, action.__doc__ or "")
                    func_defs.append(func_def)
                else:  # Action
                    func_def = await tool_def_builder(action.run_tool, action.get_description())
                    func_def.get("function")["name"] = tool_name
                    func_defs.append(func_def)
            except Exception as e:
                logger.bind(ctx=ctx, tool_name=tool_name, code=AppCode.TOOL_FACTORY_OPENROUTER).exception(
                    f"Error in get_chat_completion_tool_param (OpenRouter) for {tool_name}: {str(e)}"
                )

        return func_defs if len(func_defs) > 0 else None

    @staticmethod
    async def get_mcp_chat_completion_tool_param(
        ctx: Context,
        mcp_docs: List[MCPDoc],
    ) -> tuple[List[ChatCompletionToolParam], dict[str, MCPToolBinding]]:
        """List tools on each MCP server and expose them as OpenAI function tools.

        OpenRouter has no server-side MCP support, so remote tools are advertised as
        ordinary functions and executed by us when called. Servers are queried
        concurrently; a server that fails to respond is skipped so the rest of the
        turn still proceeds.

        Returns the tool params alongside a mapping from the namespaced tool name to
        the binding needed to execute it.
        """

        async def _load(mcp_doc: MCPDoc) -> tuple[List[ChatCompletionToolParam], dict[str, MCPToolBinding]]:
            tool_params: List[ChatCompletionToolParam] = []
            bindings: dict[str, MCPToolBinding] = {}
            try:
                mcp_tools = await MCPClientWrapper.list_tools(
                    ctx=ctx,
                    server_url=mcp_doc.server_url,
                    server_type=mcp_doc.server_type,
                    oauth_token=mcp_doc.oauth_token,
                    request_headers=mcp_doc.request_headers,
                    allowed_tools=mcp_doc.allowed_tools,
                )
            except Exception as e:
                logger.bind(ctx=ctx, mcp_server_name=mcp_doc.server_name).warning(
                    f"Skipping MCP server {mcp_doc.server_name}, could not list tools: {str(e)}"
                )
                return tool_params, bindings

            for mcp_tool in mcp_tools:
                tool_param = MCPClientWrapper.to_chat_completion_tool_param(
                    mcp_tool=mcp_tool,
                    server_name=mcp_doc.server_name,
                )
                tool_params.append(tool_param)
                bindings[tool_param["function"]["name"]] = MCPToolBinding(
                    server_name=mcp_doc.server_name,
                    server_url=mcp_doc.server_url,
                    server_type=mcp_doc.server_type,
                    oauth_token=mcp_doc.oauth_token,
                    request_headers=mcp_doc.request_headers,
                    mcp_tool_name=mcp_tool.name,
                )

            logger.bind(ctx=ctx, mcp_server_name=mcp_doc.server_name).info(
                f"Loaded {len(tool_params)} MCP tools from {mcp_doc.server_name}"
            )
            return tool_params, bindings

        if not mcp_docs:
            return [], {}

        results = await asyncio.gather(*[_load(doc) for doc in mcp_docs])

        all_tool_params: List[ChatCompletionToolParam] = []
        all_bindings: dict[str, MCPToolBinding] = {}
        for tool_params, bindings in results:
            all_tool_params.extend(tool_params)
            all_bindings.update(bindings)

        return all_tool_params, all_bindings


    @staticmethod
    def _extract_operation(tool_args: object) -> str | None:
        if tool_args is None:
            return None

        if isinstance(tool_args, dict):
            op = tool_args.get("operation")
            return str(op).strip() if op is not None else None

        if isinstance(tool_args, str):
            try:
                parsed = json.loads(tool_args)
            except Exception:
                return None
            if isinstance(parsed, dict):
                op = parsed.get("operation")
                return str(op).strip() if op is not None else None
            return None

        op = getattr(tool_args, "operation", None)
        if op is None:
            return None
        return str(getattr(op, "value", op)).strip()

    @staticmethod
    async def maybe_save_output_to_file(
        ctx: Context,
        user: UserORM,
        tool_output: DataBlock,
        tool_name: str,
        tool_args,
        notify: Notify | None = None,
        output_text_to_file_char_limit: int | None = None,
    ) -> DataBlock:
        tool_name = str(tool_name or "").strip()

        if tool_name == "openai_vector_store_tool":
            op = ToolFactoryOpenRouter._extract_operation(tool_args)
            if op in ToolFactoryOpenRouter.NO_OFFLOAD_VECTOR_STORE_OPS:
                return tool_output

        if (
            output_text_to_file_char_limit
            and tool_output.text
            and len(tool_output.text) > output_text_to_file_char_limit
            # and tool_name != "read_url"
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
            logger.bind(
                ctx=ctx,
                code=AppCode.TOOL_FACTORY_OPENROUTER,
                tool_name=tool_name,
            ).info(f"Converted tool output to file url: {str(file_meta.url)}")

            merged_urls: list[str] = []
            seen: set[str] = set()
            for u in list(getattr(tool_output, "urls", None) or []):
                s = str(u or "").strip()
                if s and s not in seen:
                    seen.add(s)
                    merged_urls.append(s)
            if file_meta.url:
                s = str(file_meta.url).strip()
                if s and s not in seen:
                    seen.add(s)
                    merged_urls.append(s)

            return DataBlock(
                text=tool_output.text,
                urls=merged_urls,
                file_ids=tool_output.file_ids,
            )

        return tool_output

    @staticmethod
    async def maybe_save_tool_input_to_file_urls(
        ctx: Context,
        user: UserORM | None,
        tool_name: str,
        tool_args: Any,
        notify: Notify | None = None,
        input_text_to_file_char_limit: int | None = None,
    ) -> tuple[Any, list[str]]:
        
        if not input_text_to_file_char_limit:
            return tool_args, []
        if not user:
            return tool_args, []

        tool_name = str(tool_name or "").strip()
        if tool_name not in ToolFactoryOpenRouter.OFFLOAD_TOOL_INPUT_ALLOWLIST:
            return tool_args, []

        if tool_args is None:
            return tool_args, []

        s3 = get_public_s3()
        user_files = UserFiles(s3=s3, user=user)
        created_urls: list[str] = []

        async def _offload_str(s: str, path: str) -> str:
            filename = str(gen_uuid()) + ".txt"
            notify(ctx=ctx, message="Tool input too long, saving to file") if notify else None
            file_metas = await user_files.upload_binary(
                ctx=ctx, file_obj=BytesIO(s.encode("utf-8")), filename=filename
            )
            file_meta = file_metas[0]
            url = str(file_meta.url) if file_meta.url else ""
            if url:
                created_urls.append(url)
                logger.bind(
                    ctx=ctx,
                    code=AppCode.TOOL_FACTORY_OPENROUTER,
                    tool_name=tool_name,
                    tool_input_url=url,
                    tool_input_char_length=len(s),
                ).info("Converted tool input string to file url")
                return url

            logger.bind(
                ctx=ctx,
                code=AppCode.TOOL_FACTORY_OPENROUTER,
                tool_name=tool_name,
                tool_input_path=path,
                tool_input_char_length=len(s),
            ).warning(
                "Tool input string exceeded limit but could not be saved to file url; truncating"
            )
            return s[:1000] + "..."

        async def _walk(v: Any, path: str) -> Any:
            if isinstance(v, str):
                if len(v) > input_text_to_file_char_limit:
                    return await _offload_str(v, path)
                return v
            if isinstance(v, dict):
                out: dict[Any, Any] = {}
                for k, vv in v.items():
                    key_str = str(k)
                    child_path = f"{path}.{key_str}" if path != "$" else f"$.{key_str}"
                    out[k] = await _walk(vv, child_path)
                return out
            if isinstance(v, list):
                out_list: list[Any] = []
                for i, x in enumerate(v):
                    child_path = f"{path}[{i}]"
                    out_list.append(await _walk(x, child_path))
                return out_list
            return v

        # If tool_args is a JSON string, sanitize within the parsed structure and dump back to JSON
        if isinstance(tool_args, str):
            try:
                parsed = json.loads(tool_args)
            except Exception:
                sanitized = await _walk(tool_args, "$")
                return sanitized, created_urls

            if isinstance(parsed, (dict, list)):
                sanitized_obj = await _walk(parsed, "$")
                try:
                    return json.dumps(sanitized_obj), created_urls
                except Exception:
                    return tool_args, created_urls

            sanitized_scalar = await _walk(parsed, "$")
            if isinstance(sanitized_scalar, str):
                return sanitized_scalar, created_urls
            try:
                return json.dumps(sanitized_scalar), created_urls
            except Exception:
                return tool_args, created_urls

        sanitized_args = await _walk(tool_args, "$")
        return sanitized_args, created_urls

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
        if self.user is None:
            raise ValueError("ToolFactoryOpenRouter requires user")

        tool_factory = ToolFactory(user=self.user, db=self.db)


        # Defensive: ToolFactory expects JSON when tool_args is a string, and some OpenRouter providers
        # reject non-JSON tool_calls[].function.arguments. If a model emits malformed JSON, wrap it so
        # the tool runner can proceed (it may still fail schema validation, but won't poison the run).
        if isinstance(tool_args, str):
            try:
                json.loads(tool_args)
            except Exception:
                tool_args = json.dumps({"raw": tool_args}, ensure_ascii=False, default=str)

        # Run underlying tool without output offloading.
        tool_output = await tool_factory.run_tool(
            ctx,
            app_state,
            tool_name,
            tool_args,
            notify=notify,
        )

        # OpenRouter-only output offloading pass.
        tool_output = await ToolFactoryOpenRouter.maybe_save_output_to_file(
            ctx=ctx,
            user=self.user,
            tool_output=tool_output,
            tool_name=str(tool_name),
            tool_args=tool_args,
            notify=notify,
            output_text_to_file_char_limit=output_text_to_file_char_limit,
        )

        logger.bind(
            ctx=ctx,
            code=AppCode.TOOL_FACTORY_OPENROUTER,
            tool_name=tool_name,
            tool_args=repr(tool_args)[:300],
            tool_output=repr(tool_output)[:200],
        ).info(f"Ran tool: {tool_name}")

        return tool_output
