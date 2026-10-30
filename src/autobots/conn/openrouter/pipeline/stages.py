from typing import Any, ClassVar

from fastapi import HTTPException
from loguru import logger

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.openrouter.chat_model import OpenRouterChatReq
from src.autobots.conn.openrouter.image_handler import (
    build_multimodal_user_message,
    detect_image_urls,
    extract_text_from_message,
)
from src.autobots.conn.openrouter.openrouter_chat import OpenRouterChat
from src.autobots.conn.openrouter.pipeline.turn_pipeline import TurnContext, TurnStage
from src.autobots.conn.openrouter.runtime_context import (
    build_runtime_context,
    inject_runtime_context,
    strip_runtime_context,
)
from src.autobots.conn.openrouter.utils import dedupe_urls, is_tool_present, to_plain
from src.autobots.conn.openrouter.vector_store.history_indexer import HistoryVectorStoreIndexer
from src.autobots.core.logging.app_code import AppCode
from src.autobots.core.settings import SettingsProvider
from src.autobots.core.utils import gen_uuid


class StateInitStage(TurnStage):
    name: ClassVar[str] = "state_init"

    async def run(self, turn: TurnContext) -> None:
        action = turn.action
        if not getattr(action.action_config, "chat_turn", None):
            action.action_config.chat_turn = 1
        if not getattr(action.action_config, "session_id", None):
            action.action_config.session_id = str(gen_uuid())
        turn.current_turn = action._current_turn()


class ModelCapabilityStage(TurnStage):
    name: ClassVar[str] = "model_capability"

    async def run(self, turn: TurnContext) -> None:
        await turn.action._validate_and_adjust_model_capabilities(ctx=turn.ctx, notify=turn.notify)


class ReasoningStripStage(TurnStage):
    name: ClassVar[str] = "reasoning_strip"

    async def run(self, turn: TurnContext) -> None:
        turn.action._strip_reasoning_fields_from_persisted_state()


class WindowRebuildStage(TurnStage):
    name: ClassVar[str] = "window_rebuild"

    async def run(self, turn: TurnContext) -> None:
        action = turn.action
        action._ensure_pinned_messages_initialized()
        action._rebuild_working_messages_from_history()
        turn.turn_start_index = len(action.action_config.messages or [])
        if turn.app_state.cancellation is not None:
            turn.app_state.cancellation.set_snapshot_meta(
                turn_start_index=turn.turn_start_index,
                chat_turn=int(getattr(action.action_config, "chat_turn", 1) or 1),
            )


class UserInputStage(TurnStage):
    name: ClassVar[str] = "user_input"

    async def run(self, turn: TurnContext) -> None:
        action = turn.action
        action_input = turn.action_input

        if action_input and (action_input.text != "" or action_input.urls):
            llm_prompt = action_input.get_llm_prompt(ctx=turn.ctx)
            action.action_config.messages.append({"role": "user", "content": llm_prompt})

        input_urls = dedupe_urls(list(action_input.urls or []))
        action.action_config.current_turn_urls = input_urls
        all_turn_urls = dedupe_urls(
            list(getattr(action.action_config, "all_turn_urls", None) or []) + input_urls
        )
        action.action_config.all_turn_urls = all_turn_urls[-2000:]

        turn.previous_tool_results = to_plain(
            getattr(action.action_config, "tool_results", None) or []
        )


class ToolResolutionStage(TurnStage):
    name: ClassVar[str] = "tool_resolution"

    async def run(self, turn: TurnContext) -> None:
        action = turn.action
        effective_tools: list[Any] = []
        effective_tools.extend(list(getattr(action.action_config, "tools", None) or []))
        effective_tools.extend(list(turn.action_input.tools or []))

        tool_defs = await action._replace_tools_with_defs(
            ctx=turn.ctx,
            tools=effective_tools,
            model_id=getattr(action.action_config, "model", None),
        )
        action.action_config.tools = to_plain(tool_defs or [])
        turn.tool_defs = tool_defs


class VectorStoreStage(TurnStage):
    name: ClassVar[str] = "vector_store"

    async def run(self, turn: TurnContext) -> None:
        action = turn.action
        if not (
            getattr(action.action_config, "vector_store_enabled", True)
            and is_tool_present(getattr(action.action_config, "tools", None), "openai_vector_store_tool")
        ):
            action.action_config.current_turn_vector_store_urls = []
            return

        try:
            await action._ensure_vector_store_exists(ctx=turn.ctx, app_state=turn.app_state, notify=turn.notify)
            await action._prepare_vector_store_state(
                ctx=turn.ctx, app_state=turn.app_state, action_input=turn.action_input
            )
            await action._auto_index_current_turn_urls_to_vector_store(ctx=turn.ctx, app_state=turn.app_state, notify=turn.notify)
        except Exception as e:
            logger.bind(ctx=turn.ctx, error=str(e)).warning(
                "OpenAI vector store unavailable; continuing without it for this run"
            )
            if turn.notify:
                turn.notify(
                    turn.ctx,
                    "Note: file search (OpenAI vector store) is unavailable; continuing without it.",
                )
            action.action_config.current_turn_vector_store_urls = []
            turn.tool_defs = action._remove_tool_by_name(turn.tool_defs, "openai_vector_store_tool")


class McpResolveStage(TurnStage):
    name: ClassVar[str] = "mcp_resolve"

    async def run(self, turn: TurnContext) -> None:
        tool_defs, mcp_tool_bindings = await turn.action._resolve_mcp_tool_defs(
            ctx=turn.ctx, tool_defs=turn.tool_defs
        )
        turn.tool_defs = tool_defs
        turn.mcp_tool_bindings = mcp_tool_bindings


class BuildRequestStage(TurnStage):
    name: ClassVar[str] = "build_request"

    async def run(self, turn: TurnContext) -> None:
        action = turn.action
        payload = action.action_config.model_dump(exclude_none=True)
        payload["tools"] = to_plain(turn.tool_defs or [])
        payload["tool_results"] = []
        payload["tool_results_history"] = turn.previous_tool_results

        runtime_text = build_runtime_context(action.action_config, turn.action_input)
        if runtime_text:
            payload["messages"] = inject_runtime_context(payload.get("messages") or [], runtime_text)

        if payload.get("supports_image"):
            turn_urls = payload.get("current_turn_urls") or []
            image_urls = detect_image_urls(turn_urls)
            msgs = payload.get("messages") or []
            if image_urls and msgs:
                last_msg = msgs[-1]
                if isinstance(last_msg, dict) and last_msg.get("role") == "user":
                    text = extract_text_from_message(last_msg)
                    msgs[-1] = build_multimodal_user_message(text=text, image_urls=image_urls)
                    payload["messages"] = msgs

        payload.pop("tool_output_text_to_file_char_limit", None)
        payload.pop("tool_input_text_to_file_char_limit", None)
        payload.pop("supports_image", None)

        if payload.get("session_id") is not None:
            payload["session_id"] = str(payload.get("session_id"))

        turn.chat_req = OpenRouterChatReq.model_validate(payload)


class ExecuteStage(TurnStage):
    name: ClassVar[str] = "execute"

    async def run(self, turn: TurnContext) -> None:
        action = turn.action
        chat_req = turn.chat_req

        openrouter_conn = await AppConn.get_openrouter(ctx=turn.ctx, db=action.db, user=action.user)
        if isinstance(openrouter_conn, Exception):
            raise openrouter_conn

        if openrouter_conn.is_system_creds and chat_req.model:
            settings = SettingsProvider.sget()
            prefixes = settings.OPENROUTER_SYSTEM_ALLOWED_MODEL_PREFIXES
            if prefixes and not any(chat_req.model.startswith(p) for p in prefixes):
                original_model = chat_req.model
                chat_req.model = settings.OPENROUTER_SYSTEM_FALLBACK_MODEL
                logger.bind(
                    ctx=turn.ctx,
                    original_model=original_model,
                    fallback_model=chat_req.model,
                ).info("System creds: auto-fallback to allowed model")

        runner = OpenRouterChat(client=openrouter_conn.client)
        runner.register_mcp_tool_bindings(turn.mcp_tool_bindings)

        max_continue_retries = 5
        chat_res = None
        assistant_texts: list[str] = []

        for attempt in range(max_continue_retries + 1):
            try:
                chat_res = await runner.chat(
                    app_state=turn.app_state,
                    chat_req=chat_req,
                    user=action.user,
                    ctx=turn.ctx,
                    db=action.db,
                    notify=turn.notify,
                    tool_output_text_to_file_char_limit=action.action_config.tool_output_text_to_file_char_limit,
                    tool_input_text_to_file_char_limit=action.action_config.tool_input_text_to_file_char_limit,
                )
            except HTTPException as exc:
                if (
                    exc.status_code == 502
                    and "Empty streamed completion from OpenRouter" in str(getattr(exc, "detail", ""))
                    and attempt < max_continue_retries
                ):
                    msg = {"role": "user", "content": "Continue"}
                    chat_req.messages.append(msg)
                    continue
                raise

            if not chat_res:
                return

            assistant_texts = []
            for choice in chat_res.choices or []:
                content = (
                    getattr(choice.message, "content", None)
                    if choice and getattr(choice, "message", None)
                    else None
                )
                if content:
                    assistant_texts.append(content)

            if assistant_texts:
                break

            if attempt >= max_continue_retries:
                choice_summaries: list[dict[str, Any]] = []
                for ch in chat_res.choices or []:
                    try:
                        msg = getattr(ch, "message", None)
                        tool_calls = getattr(msg, "tool_calls", None) if msg is not None else None
                        choice_summaries.append(
                            {
                                "finish_reason": getattr(ch, "finish_reason", None),
                                "has_content": bool(getattr(msg, "content", None) if msg is not None else None),
                                "has_refusal": bool(getattr(msg, "refusal", None) if msg is not None else None),
                                "tool_calls_count": len(tool_calls or []) if isinstance(tool_calls, list) else 0,
                                "has_function_call": bool(getattr(msg, "function_call", None) if msg is not None else None),
                            }
                        )
                    except Exception:
                        continue

                logger.bind(
                    ctx=turn.ctx,
                    app_code=AppCode.ACTION,
                    model=str(getattr(chat_req, "model", "")),
                    stream=bool(getattr(chat_req, "stream", False)),
                    choice_summaries=choice_summaries,
                ).warning("OpenRouter chat completion contained no assistant text")

                raise HTTPException(
                    status_code=503,
                    detail="No response text from OpenRouter chat completion",
                )

            msg = {"role": "user", "content": "Continue"}
            chat_req.messages.append(msg)

        turn.assistant_texts = assistant_texts
        turn.mcp_tool_bindings = runner.mcp_tool_bindings


class PersistStage(TurnStage):
    name: ClassVar[str] = "persist"

    async def run(self, turn: TurnContext) -> None:
        action = turn.action
        chat_req = turn.chat_req

        action._persist_mcp_references(turn.mcp_tool_bindings)

        current_turn_tool_results = to_plain(getattr(chat_req, "tool_results", None) or [])
        merged_tool_results = action._merge_tool_results_history(
            turn.previous_tool_results, current_turn_tool_results
        )
        action.action_config.tool_results = merged_tool_results

        action.action_config.current_turn_urls = to_plain(
            getattr(chat_req, "current_turn_urls", None) or []
        )
        action.action_config.all_turn_urls = to_plain(
            getattr(chat_req, "all_turn_urls", None) or []
        )

        messages_clean = strip_runtime_context(
            to_plain(getattr(chat_req, "messages", None) or [])
        )
        action.action_config.messages = messages_clean
        appended = action._append_current_turn_to_history(
            turn=turn.current_turn, start_index=turn.turn_start_index
        )

        action._rebuild_working_messages_from_history()

        if appended:
            action.action_config.chat_turn = turn.current_turn + 1

        for content in turn.assistant_texts:
            turn.data_blocks.blocks.append(DataBlock(text=content))


class HistoryIndexStage(TurnStage):
    name: ClassVar[str] = "history_index"

    async def run(self, turn: TurnContext) -> None:
        action = turn.action
        cfg = action.action_config

        if not getattr(cfg, "history_vector_store_enabled", True):
            return

        every_n = int(getattr(cfg, "history_vector_store_index_every_n_turns", 20) or 20)
        visible = int(getattr(cfg, "visible_turns", 20) or 20)
        every_n = min(every_n, visible)

        current_turn = int(getattr(cfg, "chat_turn", 1) or 1)
        if current_turn <= 0 or current_turn % every_n != 0:
            return

        last_indexed = int(getattr(cfg, "history_vector_store_last_indexed_turn", 0) or 0)
        if current_turn <= last_indexed:
            return

        try:
            await HistoryVectorStoreIndexer.ensure_store_exists(
                ctx=turn.ctx,
                app_state=turn.app_state,
                user=action.user,
                db=action.db,
                notify=turn.notify,
                chat_req=cfg,
            )

            history = to_plain(getattr(cfg, "turn_history", None) or [])
            batch = [
                t
                for t in history
                if isinstance(t, dict) and last_indexed < int(t.get("turn") or 0) <= current_turn
            ]
            if not batch:
                cfg.history_vector_store_last_indexed_turn = current_turn
                return

            await HistoryVectorStoreIndexer.index_batch(
                ctx=turn.ctx,
                app_state=turn.app_state,
                user=action.user,
                db=action.db,
                notify=turn.notify,
                history_vector_store_id=cfg.history_vector_store_id,
                turns=batch,
                turn_start=last_indexed + 1,
                turn_end=current_turn,
            )
            cfg.history_vector_store_last_indexed_turn = current_turn
            logger.bind(
                ctx=turn.ctx,
                turn_start=last_indexed + 1,
                turn_end=current_turn,
                history_vector_store_id=getattr(cfg, "history_vector_store_id", None),
            ).info("Indexed conversation turn-history batch into vector store")
        except Exception as exc:
            logger.bind(ctx=turn.ctx, current_turn=current_turn).warning(
                "Failed indexing conversation turn-history batch into vector store: {}", exc
            )
