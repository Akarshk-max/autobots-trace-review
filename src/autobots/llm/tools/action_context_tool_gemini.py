from typing import Any, Type

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
from src.autobots.conn.gemini.context_tool.context_tool_models import (
    GeminiContextQueryReq,
    GeminiContextQueryRes,
)
from src.autobots.conn.gemini.context_tool.context_tool_service import GeminiContextToolService
from src.autobots.conn.gemini.file_search_store.file_search_store_client import (
    GeminiFileSearchStoreClient,
)
from src.autobots.conn.gemini.model_registry_gemini import GeminiModelRegistry
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify


class ActionContextToolGemini(
    ActionABC[
        GeminiContextQueryReq,
        GeminiContextQueryReq,
        GeminiContextQueryReq,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.action_text2text_context_tool_gemini

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return GeminiContextQueryReq

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return GeminiContextQueryReq

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return GeminiContextQueryReq

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
Unified Gemini context and memory retrieval tool.

Use this tool to access conversation memory and prior tool outputs.
It is intentionally sophisticated and supports exact ground-truth retrieval.

IMPORTANT — call this proactively, on EVERY personal or factual question,
not only ones that reference timing. The user cannot see your context window
and does not know which turns have scrolled out of it — a bare question like
"what is my cat's name?" or "remember my X?" is just as likely to be asking
about something outside your visible window as one that says "you told me
this earlier." Treat any question about a name, preference, decision, or
fact you don't currently see as a trigger to call this tool with
`semantic_query` FIRST. Never answer "I don't have that information" or
"you haven't told me that" without calling this tool first — it may have
been said earlier and simply fallen outside what you can currently see.
Only tell the user you have no record of something after this tool has
returned no results.

When to use which operation
---------------------------
1. `get_turns`
   Use this for exact turn-by-turn ground truth from persisted chat history.
   Best when you know the turn number, a turn range, a role filter, or you need
   exact wording from prior conversation.

2. `get_tool_results`
   Use this for exact ground truth from prior tool calls.
   Best for checking what a tool returned earlier, whether it failed, whether it
   generated files, and which URLs/files were produced.

3. `stats`
   Returns counts and availability of the context sources.

4. Semantic search (set `semantic_query` instead of `operation`)
   Use this when you need to recall something from much earlier in the
   conversation that has fallen outside the visible turn window, and you
   don't know an exact turn number or exact wording to search for. The
   backend periodically indexes completed turns into a conversation-memory
   vector store (every `history_vector_store_index_every_n_turns` turns,
   default 20). Set `semantic_query` to a natural-language question; when set
   it takes priority over `operation`/turn filtering. Returns at most
   `semantic_max_results` (default 5, max 20) chunks. If no turns have been
   indexed yet (conversation still short), this returns no results — fall
   back to `get_turns` instead.

Important guidance
------------------
- Prefer `get_turns` or `get_tool_results` when you need exact factual ground truth.
- Prefer `semantic_query` when you need to recall older context and don't know exactly where to look.
"""

    @classmethod
    def build_request_with_runtime_context(
        cls,
        *,
        req: GeminiContextQueryReq,
        turn_history: list[dict[str, Any]] | None,
        tool_results: list[dict[str, Any]] | None,
        pinned_messages: list[dict[str, Any]] | None = None,
    ) -> GeminiContextQueryReq:
        return req.set_runtime_context(
            turn_history=turn_history,
            tool_results=tool_results,
            pinned_messages=pinned_messages,
        )

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
                        text=GeminiContextToolService.result_to_text(result),
                    )
                ]
            )
        except Exception as exc:
            logger.bind(ctx=ctx).exception("Error in ActionContextToolGemini.run_action: {}", exc)
            return exc

    async def run_tool(
        self,
        action_config: GeminiContextQueryReq,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> GeminiContextQueryRes | Exception:
        try:
            gemini_conn = await AppConn.get_gemini(ctx=ctx, db=self.db, user=self.user)
            if isinstance(gemini_conn, Exception):
                raise gemini_conn

            fs_client = GeminiFileSearchStoreClient(gemini_client=gemini_conn)
            return await GeminiContextToolService.execute(
                action_config,
                fs_client=fs_client,
                model=GeminiModelRegistry.default_model(),
            )
        except Exception as exc:
            logger.bind(ctx=ctx).exception("Error in ActionContextToolGemini.run_tool: {}", exc)
            return exc
