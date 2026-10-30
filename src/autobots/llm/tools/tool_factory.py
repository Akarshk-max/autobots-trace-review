import inspect
import json
from io import BytesIO
from typing import Any, List

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai.types import beta, responses
from openai.types.chat import ChatCompletionToolParam
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.openai.openai_function_definition_gen import OpenAIFunctionDefinitionGen
from src.autobots.core.logging.app_code import AppCode
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.llm.tools.tools_map import TOOLS_MAP
from src.autobots.mcp.mcp_doc_model import MCP_START_CONSTANT
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


class ToolFactory:
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
    async def get_function_tool_param(
        ctx: Context, tool_names: List[str]
    ) -> List[beta.FunctionToolParam] | None:
        func_defs = []
        for tool_name in tool_names:
            # logger.bind(ctx=ctx, code=AppCode.TOOL_FACTORY, tool_name=tool_name).info(
            #     "Starting get_function_tool_param for tool"
            # )
            try:
                action = TOOLS_MAP.get(tool_name)
                # if callable(action):
                if inspect.isfunction(action):
                    func_def = await OpenAIFunctionDefinitionGen.get_function_tool_param(
                        action, action.__doc__
                    )
                    func_defs.append(func_def)
                else:  # Action
                    func_def = await OpenAIFunctionDefinitionGen.get_function_tool_param(
                        action.run_tool, action.get_description()
                    )
                    func_def.get("function")["name"] = tool_name
                    func_defs.append(func_def)
            except Exception as e:
                logger.bind(ctx=ctx, code=AppCode.TOOL_FACTORY, tool_name=tool_name).error(
                    f"Error in get_function_tool_param: {str(e)}"
                )
        return func_defs if len(func_defs) > 0 else None

    @staticmethod
    async def get_chat_completion_tool_param(
        ctx: Context, tool_names: List[str]
    ) -> List[ChatCompletionToolParam] | None:
        func_defs = []
        for tool_name in tool_names:
            # logger.bind(ctx=ctx, code=AppCode.TOOL_FACTORY, tool_name=tool_name).info(
            #     "Starting get_chat_completion_tool_param for tool"
            # )
            try:
                action = TOOLS_MAP.get(tool_name)
                if inspect.isfunction(action):
                    # if callable(action):
                    func_def = await OpenAIFunctionDefinitionGen.get_chat_completion_tool_param(
                        action, action.__doc__
                    )
                    func_defs.append(func_def)
                else:  # Action
                    func_def = await OpenAIFunctionDefinitionGen.get_chat_completion_tool_param(
                        action.run_tool, action.get_description()
                    )
                    func_def.get("function")["name"] = tool_name
                    func_defs.append(func_def)
            except Exception as e:
                logger.bind(ctx=ctx, code=AppCode.TOOL_FACTORY, tool_name=tool_name).exception(
                    f"Error in get_chat_completion_tool_param for {tool_name}: {str(e)}"
                )

        return func_defs if len(func_defs) > 0 else None

    @staticmethod
    async def get_responses_function_tool_params(
        ctx: Context, tool_names: List[str | Any]
    ) -> List[responses.FunctionToolParam] | None:
        func_defs = []
        for i, tool_name in enumerate(tool_names):
            # logger.bind(ctx=ctx, code=AppCode.TOOL_FACTORY, tool_name=tool_name).info(
            #     "Starting get_responses_tool_param for tool"
            # )
            if isinstance(tool_name, str) and (
                tool_name == "code_interpreter"
                or tool_name == "file_search"
                or tool_name == "web_search"
                or (len(tool_name) > 4 and tool_name.startswith(MCP_START_CONSTANT))
            ):
                # logger.bind(ctx=ctx, code=AppCode.TOOL_FACTORY, tool_name=tool_name).info(
                #     f"Using built-in tool definition for {tool_name}"
                # )
                func_defs.append(tool_name)
                continue
            elif isinstance(tool_name, dict):
                func_defs.append(tool_name)
                continue
            try:
                action = TOOLS_MAP.get(tool_name)
                if inspect.isfunction(action):
                    func_def = await OpenAIFunctionDefinitionGen.get_responses_function_tool_param(
                        action, action.__doc__
                    )
                    func_defs.append(func_def)
                else:  # Action
                    func_def = await OpenAIFunctionDefinitionGen.get_responses_function_tool_param(
                        action.run_tool, action.get_description()
                    )
                    func_def["name"] = tool_name
                    func_defs.append(func_def)
            except Exception as e:
                logger.bind(ctx=ctx, code=AppCode.TOOL_FACTORY, tool_name=tool_name).exception(
                    f"Error in get_responses_tool_param for {tool_name}: {str(e)}"
                )

        return func_defs if len(func_defs) > 0 else None

    async def run_tool(
        self,
        ctx: Context,
        app_state: AppState,
        tool_name: str,
        tool_args,
        notify: Notify | None = None,
        output_text_to_file_char_limit: int | None = None,
        raise_on_error: bool = False,
    ) -> DataBlock:
        app_state.raise_if_cancelled()
        logger.bind(ctx=ctx, code=AppCode.TOOL_FACTORY, tool_name=tool_name).info(
            f"Running tool: {tool_name}"
        )
        notify(ctx, f"Using {tool_name} ") if notify else None
        # tool_args is 1st param of tool_name
        tool_output = DataBlock()
        try:
            action_type = TOOLS_MAP.get(tool_name)
            # if callable(action_type):
            if inspect.isfunction(action_type):  # check if callable is a function
                config = tool_args
                if isinstance(config, str):
                    config = json.loads(tool_args)
                signature = inspect.signature(action_type)

                is_param_config = False
                is_param_user = False
                is_param_db = False
                is_param_notify = False
                is_param_space_member = False
                is_param_app_state = False
                for param_name, param in signature.parameters.items():
                    if param_name == "config":
                        is_param_config = True
                        config = param.annotation.model_validate(config)
                    if param_name == "user":
                        is_param_user = True
                    if param_name == "db":
                        is_param_db = True
                    if param_name == "notify":
                        is_param_notify = True
                    if param_name == "space_member":
                        is_param_space_member = True
                    if param_name == "app_state":
                        is_param_app_state = True

                # Build kwargs dynamically based on detected params
                kwargs = {"ctx": ctx}
                if is_param_config:
                    kwargs["config"] = config
                if is_param_user:
                    kwargs["user"] = self.user
                if is_param_db:
                    kwargs["db"] = self.db
                if is_param_notify:
                    kwargs["notify"] = notify
                if is_param_space_member:
                    kwargs["space_member"] = self.user.space_member if self.user else None
                if is_param_app_state:
                    kwargs["app_state"] = app_state

                output = None
                if is_param_config:
                    output = await action_type(**kwargs)
                else:
                    output = "Error: Invalid parameters"

                match output:
                    case Exception():
                        if raise_on_error:
                            raise output
                        tool_output.text = str(output)
                    case BaseModel():
                        tool_output.text = output.model_dump_json(exclude_none=True)

                    case str():
                        tool_output.text = output
                    case _:
                        tool_output.text = json.dumps(output)

                match output:
                    case BaseModel():
                        output_dict = output.model_dump()
                        if "urls" in output_dict:
                            for url in output_dict["urls"]:
                                if url not in tool_output.urls:
                                    tool_output.urls.append(url)

                match output:
                    case BaseModel():
                        output_dict = output.model_dump()
                        if "file_ids" in output_dict:
                            logger.bind(
                                ctx=ctx,
                                code=AppCode.TOOL_FACTORY,
                                tool_name=tool_name,
                                extracted_file_ids=len(output_dict["file_ids"]),
                                file_ids_list=output_dict["file_ids"],
                            ).info(f"Extracting file_ids from tool {tool_name} output")

                            for file_id in output_dict["file_ids"]:
                                if file_id not in tool_output.file_ids:
                                    tool_output.file_ids.append(file_id)

            else:  # Action
                # action_type: ActionABC
                if isinstance(tool_args, str):
                    tool_args = json.loads(tool_args)
                config = action_type.get_config_type().model_validate(tool_args)
                action = action_type(action_config=config, db=self.db, user=self.user)
                output = await action.run_tool(action_config=config, ctx=ctx, app_state=app_state, notify=notify)
                match output:
                    case Exception():
                        if raise_on_error:
                            raise output
                        tool_output.text = str(output)
                    case _:
                        tool_output.text = output.model_dump_json(
                            exclude_none=True
                        )  # json.dumps(output.model_dump())

                match output:
                    case BaseModel():
                        output_dict = output.model_dump()
                        if "urls" in output_dict:
                            for url in output_dict["urls"]:
                                if url not in tool_output.urls:
                                    tool_output.urls.append(url)

                match output:
                    case BaseModel():
                        output_dict = output.model_dump()
                        if "file_ids" in output_dict:
                            logger.bind(
                                ctx=ctx,
                                code=AppCode.TOOL_FACTORY,
                                tool_name=tool_name,
                                extracted_file_ids=len(output_dict["file_ids"]),
                                file_ids_list=output_dict["file_ids"],
                            ).info(f"Extracting file_ids from tool {tool_name} output")

                            for file_id in output_dict["file_ids"]:
                                if file_id not in tool_output.file_ids:
                                    tool_output.file_ids.append(file_id)

            tool_output = await ToolFactory.maybe_save_output_to_file(
                ctx=ctx,
                user=self.user,
                tool_output=tool_output,
                tool_name=tool_name,
                tool_args=tool_args,
                notify=notify,
                output_text_to_file_char_limit=output_text_to_file_char_limit,
            )
            logger.bind(
                ctx=ctx, code=AppCode.TOOL_FACTORY, tool_name=tool_name, tool_args=repr(tool_args)[:300], tool_output=repr(tool_output)[:200]
            ).info(f"Ran tool: {tool_name}")
        except Exception as e:
            tool_output.text = str(e)
            logger.bind(
                ctx=ctx, code=AppCode.TOOL_FACTORY, tool_name=tool_name, tool_args=repr(tool_args)[:300], tool_output=repr(tool_output)[:200]
            ).exception(f"Error in run tool: {str(e)}")
            if raise_on_error:
                raise
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
        """
        Offload oversized tool input strings to S3 and replace them with public URLs.

        Notes:
        - Only string values are considered (nested in dict/list).
        - If tool_args is a JSON string, it is parsed, transformed, and re-dumped.
        - Intended to be called *after* tool execution so the tool runs with original args.
        """
        if not input_text_to_file_char_limit:
            return tool_args, []
        # Only offload huge tool inputs for the file system tool (args can be extremely large).
        # For all other tools, keep inputs unchanged.
        if tool_name != "file_system_tool":
            return tool_args, []

        if not user:
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
                    code=AppCode.TOOL_FACTORY,
                    tool_name=tool_name,
                    tool_input_url=url,
                    tool_input_path=path,
                    tool_input_char_length=len(s),
                ).info("Converted tool input string to file url")
                return url
            logger.bind(
                ctx=ctx,
                code=AppCode.TOOL_FACTORY,
                tool_name=tool_name,
                tool_input_path=path,
                tool_input_char_length=len(s),
            ).warning(
                "Tool input string exceeded limit but could not be saved to file url; truncating"
            )
            # Fallback: if URL missing for any reason, return truncated string to reduce context size
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
                if created_urls:
                    logger.bind(
                        ctx=ctx,
                        code=AppCode.TOOL_FACTORY,
                        tool_name=tool_name,
                        offloaded_inputs=len(created_urls),
                        offloaded_input_urls_sample=created_urls[:3],
                    ).info("Offloaded large tool input strings to file URLs")
                return sanitized, created_urls

            # Only treat dict/list as structured tool args; other JSON scalars behave like plain values
            if isinstance(parsed, (dict, list)):
                sanitized_obj = await _walk(parsed, "$")
                if created_urls:
                    logger.bind(
                        ctx=ctx,
                        code=AppCode.TOOL_FACTORY,
                        tool_name=tool_name,
                        offloaded_inputs=len(created_urls),
                        offloaded_input_urls_sample=created_urls[:3],
                    ).info("Offloaded large tool input strings to file URLs")
                try:
                    return json.dumps(sanitized_obj), created_urls
                except Exception:
                    return tool_args, created_urls

            sanitized_scalar = await _walk(parsed, "$")
            if created_urls:
                logger.bind(
                    ctx=ctx,
                    code=AppCode.TOOL_FACTORY,
                    tool_name=tool_name,
                    offloaded_inputs=len(created_urls),
                    offloaded_input_urls_sample=created_urls[:3],
                ).info("Offloaded large tool input strings to file URLs")
            # If scalar becomes a URL string, return that; otherwise re-dump to JSON string
            if isinstance(sanitized_scalar, str):
                return sanitized_scalar, created_urls
            try:
                return json.dumps(sanitized_scalar), created_urls
            except Exception:
                return tool_args, created_urls

        sanitized_args = await _walk(tool_args, "$")
        if created_urls:
            logger.bind(
                ctx=ctx,
                code=AppCode.TOOL_FACTORY,
                tool_name=tool_name,
                offloaded_inputs=len(created_urls),
                offloaded_input_urls_sample=created_urls[:3],
            ).info("Offloaded large tool input strings to file URLs")
        return sanitized_args, created_urls

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
        if (
            output_text_to_file_char_limit
            and len(tool_output.text) > output_text_to_file_char_limit
            # and tool_name != "read_url"
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
                ctx=ctx, code=AppCode.TOOL_FACTORY, tool_name=tool_name, tool_output_url=file_meta.url
            ).info(f"Converted tool output to file url: {str(file_meta.url)}")
            return DataBlock(
                text=tool_output.text,
                urls=[str(file_meta.url)] if file_meta.url else [],
                file_ids=tool_output.file_ids,
            )
        else:
            return tool_output
