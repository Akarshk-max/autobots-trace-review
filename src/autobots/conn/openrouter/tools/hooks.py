import json

from loguru import logger
from openai.types.chat import ChatCompletionToolMessageParam

from src.autobots.conn.openrouter.tools.dispatcher import ToolCallContext, ToolHook, ToolOutcome
from src.autobots.core.logging.app_code import AppCode
from src.autobots.llm.tools.tool_factory_openrouter import ToolFactoryOpenRouter

_VECTOR_STORE_MUTATIONS = {
    "create_vector_store",
    "add_files",
    "reindex_files",
    "delete_vector_store_files",
    "delete_vector_store",
}


class VectorStoreSyncHook(ToolHook):
    async def after(self, runner, call: ToolCallContext, outcome: ToolOutcome) -> None:
        if call.tool_name != "openai_vector_store_tool":
            return
        try:
            cls_args = outcome.input_payload if isinstance(outcome.input_payload, dict) else {}
            op = str(cls_args.get("operation") or "").strip()
            if op in _VECTOR_STORE_MUTATIONS:
                runner._apply_vector_store_tool_state_update(
                    chat_req=call.chat_req,
                    args=cls_args,
                    tool_output_str=str(outcome.tool_output.text or outcome.tool_output_str or ""),
                    step_index=call.step_index,
                )
        except Exception as exc:
            logger.bind(ctx=call.ctx, code=AppCode.OPEN_ROUTER, tool=call.tool_name).warning(
                f"Failed to apply vector store tool state update: {str(exc)}"
            )


class McpEnableHook(ToolHook):
    async def after(self, runner, call: ToolCallContext, outcome: ToolOutcome) -> None:
        if call.tool_name != "enable_mcp":
            return
        try:
            await runner._apply_enable_mcp_tool_injection(
                ctx=call.ctx,
                chat_req=call.chat_req,
                tool_output_str=str(outcome.tool_output.text or outcome.tool_output_str or ""),
                user=call.user,
                db=call.db,
            )
        except Exception as exc:
            logger.bind(ctx=call.ctx, tool=call.tool_name).warning(
                f"Failed to inject MCP tools after enable_mcp: {str(exc)}"
            )


class UrlMergeHook(ToolHook):
    async def after(self, runner, call: ToolCallContext, outcome: ToolOutcome) -> None:
        if outcome.tool_output.urls:
            runner._merge_all_turn_urls(call.chat_req, list(outcome.tool_output.urls))


class InputOffloadHook(ToolHook):
    async def after(self, runner, call: ToolCallContext, outcome: ToolOutcome) -> None:
        if not call.tool_input_text_to_file_char_limit or call.user is None:
            return
        try:
            sanitized_tool_args, _input_urls = await ToolFactoryOpenRouter.maybe_save_tool_input_to_file_urls(
                ctx=call.ctx,
                user=call.user,
                tool_name=call.tool_name,
                tool_args=call.tool_args,
                notify=call.notify,
                input_text_to_file_char_limit=call.tool_input_text_to_file_char_limit,
            )

            if sanitized_tool_args is None or sanitized_tool_args == call.tool_args:
                return

            allow_legacy = str(call.tool_call_id).startswith("fc_")
            runner._replace_tool_call_arguments_in_messages(
                call.chat_req,
                tool_call_id=call.tool_call_id,
                tool_args=sanitized_tool_args,
                tool_name=call.tool_name,
                allow_legacy_function_call=allow_legacy,
            )
            call.tool_args = sanitized_tool_args
            if isinstance(sanitized_tool_args, str):
                try:
                    parsed = json.loads(sanitized_tool_args)
                    if isinstance(parsed, dict):
                        outcome.input_payload = parsed
                except Exception:
                    pass
            elif isinstance(sanitized_tool_args, dict):
                outcome.input_payload = dict(sanitized_tool_args)
        except Exception as exc:
            logger.bind(ctx=call.ctx, code=AppCode.OPEN_ROUTER, tool=call.tool_name).warning(
                f"Failed to offload tool input args to file URLs: {str(exc)}"
            )


class PersistResultHook(ToolHook):
    async def after(self, runner, call: ToolCallContext, outcome: ToolOutcome) -> None:
        persisted = runner._build_persisted_tool_result(
            chat_req=call.chat_req,
            tool_call_id=call.tool_call_id,
            name=call.tool_name,
            step_index=call.step_index,
            input_payload=outcome.input_payload,
            output_text=outcome.tool_output.text,
            is_error=outcome.is_error,
            urls=list(outcome.tool_output.urls or []),
            meta={"stream": bool(call.chat_req.stream)},
        )
        runner._merge_tool_results(call.chat_req, [persisted])
        outcome.persisted = persisted


class BuildToolMessageHook(ToolHook):
    async def after(self, runner, call: ToolCallContext, outcome: ToolOutcome) -> None:
        outcome.tool_msg = ChatCompletionToolMessageParam(
            role="tool",
            content=outcome.tool_output_str,
            tool_call_id=call.tool_call_id,
        )
