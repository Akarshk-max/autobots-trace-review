import json

from fastapi import HTTPException

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.conn.openrouter.tools.dispatcher import ToolCallContext, ToolHandler, ToolOutcome
from src.autobots.llm.tools.openrouter_inspect_tool import (
    OpenRouterInspectReq,
    execute_openrouter_inspect,
)
from src.autobots.llm.tools.openai_vector_store_tool import (
    OpenAIVectorStoreToolConfig,
    OpenAIVectorStoreToolOperation,
    openai_vector_store_tool,
)
from src.autobots.llm.tools.tool_factory_openrouter import ToolFactoryOpenRouter
from src.autobots.mcp.mcp_client import MCPClientWrapper


class InspectHandler(ToolHandler):
    async def run(self, runner, call: ToolCallContext, outcome: ToolOutcome) -> None:
        if call.user is None:
            raise HTTPException(status_code=400, detail="openrouter_inspect requires user context")

        req = OpenRouterInspectReq.model_validate(outcome.input_payload or {})

        if req.op == "chat_history" and (req.semantic_query or "").strip():
            outcome.tool_output = await self._semantic_history_query(req, call)
        else:
            outcome.tool_output = self._exact_query(req, runner, call)

        outcome.tool_output = await ToolFactoryOpenRouter.maybe_save_output_to_file(
            ctx=call.ctx,
            user=call.user,
            tool_output=outcome.tool_output,
            tool_name=call.tool_name,
            tool_args=call.tool_args,
            notify=call.notify,
            output_text_to_file_char_limit=call.tool_output_text_to_file_char_limit,
        )

    @staticmethod
    def _exact_query(req: OpenRouterInspectReq, runner, call: ToolCallContext) -> DataBlock:
        turn_history, pinned = runner._get_available_turn_history(call.chat_req)
        tool_results = runner._get_available_tool_results(call.chat_req)

        res = execute_openrouter_inspect(
            req=req,
            turn_history=turn_history,
            pinned_messages=pinned,
            tool_results=tool_results,
        )

        return DataBlock(
            text=json.dumps(res.model_dump(exclude_none=True), indent=2, ensure_ascii=False)
        )

    @staticmethod
    async def _semantic_history_query(req: OpenRouterInspectReq, call: ToolCallContext) -> DataBlock:
        semantic_query = str(req.semantic_query or "").strip()
        history_vector_store_id = getattr(call.chat_req, "history_vector_store_id", None)

        if not history_vector_store_id:
            payload = {
                "total_count": 0,
                "returned_count": 0,
                "results": [],
                "summary": {
                    "note": (
                        "No conversation-memory vector store exists yet "
                        "(nothing indexed so far). Fall back to turn/text_query filtering."
                    ),
                },
            }
            return DataBlock(text=json.dumps(payload, indent=2, ensure_ascii=False))

        vs_result = await openai_vector_store_tool(
            config=OpenAIVectorStoreToolConfig(
                operation=OpenAIVectorStoreToolOperation.QUERY_VECTOR_STORE,
                vector_store_id=history_vector_store_id,
                query=semantic_query,
                max_num_results=req.semantic_max_results,
            ),
            ctx=call.ctx,
            app_state=call.app_state,
            user=call.user,
            db=call.db,
            notify=call.notify,
        )
        if isinstance(vs_result, Exception):
            raise vs_result

        vs_payload: dict = {}
        if vs_result.blocks and vs_result.blocks[0].text:
            vs_payload = json.loads(vs_result.blocks[0].text)

        vs_results = vs_payload.get("results") or []
        payload = {
            "total_count": len(vs_results),
            "returned_count": len(vs_results),
            "results": vs_results,
            "summary": {
                "mode": "semantic",
                "semantic_query": semantic_query,
                "history_vector_store_id": history_vector_store_id,
            },
        }
        return DataBlock(text=json.dumps(payload, indent=2, ensure_ascii=False))


class McpHandler(ToolHandler):
    async def run(self, runner, call: ToolCallContext, outcome: ToolOutcome) -> None:
        binding = call.mcp_tool_bindings[call.tool_name]
        mcp_result = await MCPClientWrapper.call_tool_from_binding(
            ctx=call.ctx,
            binding=binding,
            arguments=outcome.input_payload if isinstance(outcome.input_payload, dict) else {},
        )
        outcome.tool_output = DataBlock(text=mcp_result)
        outcome.tool_output = await ToolFactoryOpenRouter.maybe_save_output_to_file(
            ctx=call.ctx,
            user=call.user,
            tool_output=outcome.tool_output,
            tool_name=call.tool_name,
            tool_args=call.tool_args,
            notify=call.notify,
            output_text_to_file_char_limit=call.tool_output_text_to_file_char_limit,
        )


class GenericHandler(ToolHandler):
    async def run(self, runner, call: ToolCallContext, outcome: ToolOutcome) -> None:
        outcome.tool_output = await ToolFactoryOpenRouter(user=call.user, db=call.db).run_tool(
            call.ctx,
            call.app_state,
            call.tool_name,
            call.tool_args,
            notify=call.notify,
            output_text_to_file_char_limit=call.tool_output_text_to_file_char_limit,
        )
