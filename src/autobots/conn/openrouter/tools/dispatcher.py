import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from openai.types.chat import ChatCompletionToolMessageParam

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.conn.openrouter.chat_model import OpenRouterChatReq
from src.autobots.core.logging.app_code import AppCode
from src.autobots.core.utils import truncate_str
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.mcp.mcp_client import MCPToolBinding
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


@dataclass
class ToolCallContext:
    ctx: Context
    app_state: AppState
    chat_req: OpenRouterChatReq
    tool_call_id: str
    tool_name: str
    tool_args: Any
    step_index: int
    user: UserORM | None
    db: Any | None
    notify: Notify | None
    mcp_tool_bindings: dict[str, MCPToolBinding] = field(default_factory=dict)
    tool_output_text_to_file_char_limit: int | None = None
    tool_input_text_to_file_char_limit: int | None = None


@dataclass
class ToolOutcome:
    tool_output: DataBlock = field(default_factory=lambda: DataBlock(text=""))
    is_error: bool = False
    input_payload: dict[str, Any] | None = None
    tool_output_str: str = ""
    persisted: dict[str, Any] | None = None
    tool_msg: ChatCompletionToolMessageParam | None = None


class ToolHandler(ABC):
    @abstractmethod
    async def run(self, runner: Any, call: ToolCallContext, outcome: ToolOutcome) -> None:
        raise NotImplementedError


class ToolHook(ABC):
    @abstractmethod
    async def after(self, runner: Any, call: ToolCallContext, outcome: ToolOutcome) -> None:
        raise NotImplementedError


class ToolDispatcher:
    def __init__(self, runner: Any, handlers: dict[str, ToolHandler] | None = None, hooks: list[ToolHook] | None = None):
        self.runner = runner
        self.handlers = handlers if handlers is not None else self.default_handlers()
        self.hooks = hooks if hooks is not None else self.default_hooks()

    @staticmethod
    def default_handlers() -> dict[str, ToolHandler]:
        from src.autobots.conn.openrouter.tools.handlers import (
            GenericHandler,
            InspectHandler,
            McpHandler,
        )

        return {
            "openrouter_inspect": InspectHandler(),
            "mcp": McpHandler(),
            "generic": GenericHandler(),
        }

    @staticmethod
    def default_hooks() -> list[ToolHook]:
        from src.autobots.conn.openrouter.tools.hooks import (
            BuildToolMessageHook,
            InputOffloadHook,
            McpEnableHook,
            PersistResultHook,
            UrlMergeHook,
            VectorStoreSyncHook,
        )

        return [
            VectorStoreSyncHook(),
            McpEnableHook(),
            UrlMergeHook(),
            InputOffloadHook(),
            PersistResultHook(),
            BuildToolMessageHook(),
        ]

    def _select_handler(self, call: ToolCallContext) -> ToolHandler:
        if call.tool_name in call.mcp_tool_bindings:
            return self.handlers["mcp"]
        return self.handlers.get(call.tool_name, self.handlers["generic"])

    def _parse_args(self, call: ToolCallContext, outcome: ToolOutcome) -> None:
        if isinstance(call.tool_args, str):
            try:
                outcome.input_payload = json.loads(call.tool_args)
            except Exception:
                outcome.input_payload = {"raw": call.tool_args}
                safe_args_str = json.dumps(outcome.input_payload, ensure_ascii=False, default=str)
                allow_legacy = str(call.tool_call_id).startswith("fc_")
                self.runner._replace_tool_call_arguments_in_messages(
                    call.chat_req,
                    tool_call_id=call.tool_call_id,
                    tool_args=safe_args_str,
                    tool_name=call.tool_name,
                    allow_legacy_function_call=allow_legacy,
                )
                call.tool_args = safe_args_str
        elif isinstance(call.tool_args, dict):
            outcome.input_payload = dict(call.tool_args)
        else:
            outcome.input_payload = {"raw": str(call.tool_args)}

    async def dispatch(self, call: ToolCallContext) -> ToolOutcome:
        outcome = ToolOutcome()
        self._parse_args(call, outcome)

        handler = self._select_handler(call)
        try:
            await handler.run(self.runner, call, outcome)
            outcome.tool_output_str = outcome.tool_output.get_llm_prompt(ctx=call.ctx)
            logger.bind(
                ctx=call.ctx,
                app_code=AppCode.OPEN_ROUTER,
                tool=call.tool_name,
                tool_input=truncate_str(str(call.tool_args), limit=50),
                tool_output=truncate_str(outcome.tool_output_str, limit=50),
            ).info("Ran OpenRouter tool")
        except Exception as exc:
            outcome.is_error = True
            outcome.tool_output = DataBlock(text=str(exc))
            outcome.tool_output_str = outcome.tool_output.get_llm_prompt(ctx=call.ctx)
            logger.bind(
                ctx=call.ctx,
                code=AppCode.OPEN_ROUTER,
                tool=call.tool_name,
                tool_error=str(exc),
            ).exception("Error in OpenRouter tool_call")

        for hook in self.hooks:
            await hook.after(self.runner, call, outcome)

        return outcome
