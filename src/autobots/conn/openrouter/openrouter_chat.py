import asyncio
import json
import uuid
from typing import Any, Dict, List

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai import AsyncOpenAI, AsyncStream
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionFunctionMessageParam,
    ChatCompletionMessage,
    ChatCompletionMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionUserMessageParam,
)
from openai.types.chat.chat_completion import Choice as ChatCompletionChoice
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_fixed

from src.autobots.conn.openrouter.chat_model import OpenRouterChatReq
from src.autobots.conn.openrouter.streaming_builder import StreamingResponseBuilder
from src.autobots.conn.openrouter.tools.dispatcher import ToolCallContext, ToolDispatcher
from src.autobots.conn.openrouter.utils import dedupe_urls, strip_reasoning_fields
from src.autobots.conn.openrouter.vector_store.manifest_manager import VectorStoreManifestManager
from src.autobots.core.cancellation.run_instructions import RunInstruction, drain_instructions
from src.autobots.core.logging.app_code import AppCode
from src.autobots.core.utils import gen_uuid, truncate_str
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.chat_snapshot.provider_snapshots import snapshot_openrouter
from src.autobots.llm.tools.tool_factory_openrouter import ToolFactoryOpenRouter
from src.autobots.mcp.mcp_client import MCPToolBinding
from src.autobots.mcp.user_mcp import UserMCP
from src.autobots.server_sent_events.server_sent_events import Notify, NotifyPayload
from src.autobots.server_sent_events.topics import text_topic
from src.autobots.token_usage.token_usage_service import LlmSurface, TokenUsageService
from src.autobots.user.user_orm_model import UserORM


class OpenRouterChat:

    def __init__(self, *, client: AsyncOpenAI):
        self.client = client
        self.mcp_tool_bindings: dict[str, MCPToolBinding] = {}
        self.tool_dispatcher = ToolDispatcher(runner=self)

    def register_mcp_tool_bindings(self, bindings: dict[str, MCPToolBinding] | None) -> None:
        """Register MCP tools resolved for this run so the tool loop can dispatch them."""
        if bindings:
            self.mcp_tool_bindings.update(bindings)

    @staticmethod
    def _new_legacy_tool_call_id(*, step_index: int, tool_name: str) -> str:
        safe_name = str(tool_name or "tool").strip().replace(" ", "_")[:40]
        return f"fc_{int(step_index)}_{safe_name}_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _extract_function_call_parts(function_call: Any) -> tuple[str, str]:
        """Extract (name, arguments_str) from legacy `function_call` objects/dicts."""
        if function_call is None:
            return "", ""
        if hasattr(function_call, "model_dump"):
            try:
                function_call = function_call.model_dump(exclude_none=True)
            except Exception:
                function_call = {}
        if isinstance(function_call, dict):
            name = str(function_call.get("name") or "").strip()
            args = function_call.get("arguments")
            if args is None:
                args_str = ""
            elif isinstance(args, str):
                args_str = args
            else:
                try:
                    args_str = json.dumps(args, ensure_ascii=False, default=str)
                except Exception:
                    args_str = str(args)
            return name, args_str
        name = str(getattr(function_call, "name", "") or "").strip()
        args_str = str(getattr(function_call, "arguments", "") or "")
        return name, args_str

    @staticmethod
    def _has_any_assistant_output(*, content: str | None, refusal: str | None) -> bool:
        if content and str(content).strip():
            return True
        if refusal and str(refusal).strip():
            return True
        return False

    @staticmethod
    def _is_effectively_empty_stream_completion(
        *,
        content: str | None,
        refusal: str | None,
        tool_calls_count: int,
        has_function_call: bool,
    ) -> bool:
        """True if the model returned no assistant text and no actionable tool call."""
        if OpenRouterChat._has_any_assistant_output(content=content, refusal=refusal):
            return False
        if tool_calls_count > 0:
            return False
        if has_function_call:
            return False
        return True


    @staticmethod
    def _build_synthetic_max_loop_message(
        chat_req: OpenRouterChatReq,
        reason: str,
    ) -> ChatCompletion:
        text = (
            "I stopped because the OpenRouter tool loop limit was reached.\n\n"
            "I could not safely continue calling tools, so this is a partial result based on "
            "the information already available. Some parts may be incomplete.\n\n"
            f"Reason: {reason}"
        )
        return ChatCompletion.model_construct(
            id=f"synthetic-max-loop-{gen_uuid()}",
            created=0,
            model=str(chat_req.model),
            object="chat.completion",
            choices=[
                ChatCompletionChoice.model_construct(
                    index=0,
                    finish_reason="stop",
                    message=ChatCompletionMessage.model_construct(
                        role="assistant",
                        content=text,
                    ),
                )
            ],
        )

    async def _finalize_after_max_loop(
        self,
        *,
        ctx: Context,
        app_state: AppState,
        chat_req: OpenRouterChatReq,
        notify: Notify | None,
        max_loop: int,
    ) -> ChatCompletion:
        logger.bind(ctx=ctx, code=AppCode.OPEN_ROUTER).warning(
            "OpenRouter max loop reached; forcing final no-tool response model={} max_loop={} messages={}",
            chat_req.model,
            max_loop,
            len(chat_req.messages),
        )

        if notify:
            notify(
                ctx,
                "OpenRouter reached the tool loop limit; generating final response without tools.",
            )

        chat_req.messages.append({
            "role": "user",
            "content": (
                "The tool loop limit has been reached. Do not call any more tools. "
                "Give the best final answer possible using only the information already available. "
                "Clearly separate: "
                "1) what has already been completed, "
                "2) what is still incomplete or uncertain, "
                "3) the recommended next steps. "
                "At the end, ask the user whether they want to continue from this point. "
                "Do not claim that you will continue automatically; only explain that continuation requires the user's next message."
            ),
        })

        try:
            final_config = await OpenRouterChat.get_model_based_config(ctx=ctx, chat_req=chat_req)
            final_config["tool_choice"] = "none"

            use_stream = bool(getattr(chat_req, "finalize_stream", True))
            if not use_stream:
                final_completion = await self.client.chat.completions.create(**final_config)
                TokenUsageService.record(
                    ctx=ctx,
                    surface=LlmSurface.openrouter,
                    app_state=app_state,
                    usage=getattr(final_completion, "usage", None),
                    model=str(getattr(final_completion, "model", None) or chat_req.model),
                    loop=max_loop + 1,
                )
                return final_completion

            final_config["stream"] = True
            notify_main = Notify(topic=text_topic(notify.topic), payload=NotifyPayload.snapshot) if notify else None
            notify_complimentary = Notify(topic=notify.topic) if notify else None
            builder = StreamingResponseBuilder(
                ctx=ctx,
                notify_main=notify_main,
                notify_complimentary=notify_complimentary,
            )
            stream = await self.client.chat.completions.create(**final_config)
            assert isinstance(stream, AsyncStream)
            async for chunk in stream:
                builder.ingest(chunk)
            final_completion = builder.build_completion()
            TokenUsageService.record(
                ctx=ctx,
                surface=LlmSurface.openrouter,
                app_state=app_state,
                usage=getattr(final_completion, "usage", None),
                model=str(getattr(final_completion, "model", None) or chat_req.model),
                loop=max_loop + 1,
            )
            return final_completion
        except Exception as exc:
            logger.bind(ctx=ctx, code=AppCode.OPEN_ROUTER).exception(
                "Failed to generate final response after OpenRouter max loop: {}",
                exc,
            )
            return self._build_synthetic_max_loop_message(
                chat_req=chat_req,
                reason=str(exc),
            )

    @staticmethod
    def _deliver_instructions(
        ctx: Context,
        chat_req: OpenRouterChatReq,
        instructions: list[RunInstruction],
        notify: Notify | None,
    ) -> None:
        for instruction in instructions:
            chat_req.messages.append({"role": "user", "content": instruction.as_user_message()})
            logger.bind(ctx=ctx, code=AppCode.OPEN_ROUTER).info(
                f"Run instruction applied: {instruction.text}"
            )
            notify(ctx, f"Instruction applied: {instruction.text}") if notify else None

    def _get_max_loop(self, chat_req: OpenRouterChatReq) -> int:
        if not chat_req.tools:
            return 20

        for tool in chat_req.tools:
            if isinstance(tool, str) and tool == "run_terminal_command":
                return 50
            if isinstance(tool, dict):
                func = tool.get("function")
                if isinstance(func, dict) and func.get("name") == "run_terminal_command":
                    return 50
            if hasattr(tool, "function") and getattr(tool.function, "name", None) == "run_terminal_command":
                return 50
            if hasattr(tool, "name") and tool.name == "run_terminal_command":
                return 50
        return 30

    @staticmethod
    def _truncate_tool_output_text(text: Any, max_chars: int = 20000) -> str | None:
        if text is None:
            return None
        if not isinstance(text, str):
            text = str(text)
        text = text.strip()
        if not text:
            return None
        if len(text) <= max_chars:
            return text
        return text[:max_chars]

    @staticmethod
    def _normalize_dict_list(items: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for raw in items or []:
            if hasattr(raw, "model_dump"):
                try:
                    raw = raw.model_dump(exclude_none=True)
                except Exception:
                    continue
            if isinstance(raw, dict):
                out.append(dict(raw))
        return out

    # OpenAI Vector Store state synchronization
    @staticmethod
    def _json_dict_or_none(text: Any) -> dict[str, Any] | None:
        if not isinstance(text, str):
            return None
        try:
            data = json.loads(text)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _looks_like_vector_store_tool_payload(payload: dict[str, Any]) -> bool:
        return any(
            key in payload
            for key in (
                "operation",
                "vector_store_id",
                "vector_store_name",
                "vector_store",
                "indexed_files",
                "successful_files",
                "reused_files",
                "failed_files",
                "batch",
                "add_result",
                "delete_result",
            )
        )

    @classmethod
    def _extract_tool_payload_dict(cls, raw: Any) -> dict[str, Any] | None:
        payload = raw if isinstance(raw, dict) else cls._json_dict_or_none(raw)
        if not isinstance(payload, dict):
            return None

        if cls._looks_like_vector_store_tool_payload(payload):
            return payload

        blocks = payload.get("blocks")
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                inner_text = block.get("text")
                inner_payload = cls._json_dict_or_none(inner_text)
                if isinstance(inner_payload, dict):
                    return inner_payload

        return payload

    @classmethod
    def _remove_vector_store_manifest_entries(
        cls,
        *,
        chat_req: OpenRouterChatReq,
        vector_store_file_ids: list[str],
    ) -> None:
        manifest = VectorStoreManifestManager.normalize(getattr(chat_req, "vector_store_manifest", None))
        chat_req.vector_store_manifest = VectorStoreManifestManager.remove_entries(
            manifest, vector_store_file_ids
        )

    @classmethod
    def _apply_vector_store_add_payload_to_state(
        cls,
        *,
        chat_req: OpenRouterChatReq,
        payload: dict[str, Any],
    ) -> None:
        if not payload:
            return

        vector_store = payload.get("vector_store") if isinstance(payload.get("vector_store"), dict) else {}
        chat_req.vector_store_id = (
            payload.get("vector_store_id")
            or vector_store.get("id")
            or getattr(chat_req, "vector_store_id", None)
        )
        chat_req.vector_store_name = (
            payload.get("vector_store_name")
            or vector_store.get("name")
            or getattr(chat_req, "vector_store_name", None)
        )

        manifest = VectorStoreManifestManager.normalize(getattr(chat_req, "vector_store_manifest", None))
        turn = int(getattr(chat_req, "chat_turn", 0) or 0)

        indexed_files = payload.get("indexed_files", []) or []
        successful_files = payload.get("successful_files", []) or []
        reused_files = payload.get("reused_files", []) or []
        batch = payload.get("batch") if isinstance(payload.get("batch"), dict) else {}
        batch_status = str(batch.get("status") or "").strip().lower()
        default_status = "indexed" if batch_status == "completed" else "pending"

        def _is_durable_url(url: str | None) -> bool:
            value = str(url or "").strip()
            if not value:
                return False
            if value.startswith("file://"):
                return False
            return True

        successful_openai_file_ids_by_source: dict[str, str] = {
            str(item.get("source") or "").strip(): str(
                item.get("openai_file_id") or item.get("file_id") or ""
            ).strip()
            for item in successful_files
            if isinstance(item, dict)
            and str(item.get("source") or "").strip()
            and str(item.get("openai_file_id") or item.get("file_id") or "").strip()
        }
        reused_openai_file_ids_by_source: dict[str, str] = {
            str(item.get("source") or "").strip(): str(
                item.get("openai_file_id") or item.get("file_id") or ""
            ).strip()
            for item in reused_files
            if isinstance(item, dict)
            and str(item.get("source") or "").strip()
            and str(item.get("openai_file_id") or item.get("file_id") or "").strip()
        }

        indexed_by_url: dict[str, dict[str, Any]] = {}
        durable_success_urls: list[str] = []
        for raw_item in indexed_files:
            if not isinstance(raw_item, dict):
                continue
            source_url = str(raw_item.get("source_url") or "").strip()
            if not _is_durable_url(source_url):
                continue
            durable_success_urls.append(source_url)
            indexed_by_url[source_url] = dict(raw_item)

        # Ensure urls that appear only in successful/reused maps still get represented.
        for source_url, openai_file_id in successful_openai_file_ids_by_source.items():
            if not _is_durable_url(source_url):
                continue
            if source_url not in indexed_by_url:
                durable_success_urls.append(source_url)
                indexed_by_url[source_url] = {
                    "source_url": source_url,
                    "prepared_url": source_url,
                    "openai_file_id": openai_file_id,
                    "vector_store_file_id": None,
                    "status": default_status,
                }
        for source_url, openai_file_id in reused_openai_file_ids_by_source.items():
            if not _is_durable_url(source_url):
                continue
            if source_url not in indexed_by_url:
                durable_success_urls.append(source_url)
                indexed_by_url[source_url] = {
                    "source_url": source_url,
                    "prepared_url": source_url,
                    "openai_file_id": openai_file_id,
                    "vector_store_file_id": None,
                    "status": default_status,
                }

        durable_success_urls = dedupe_urls(durable_success_urls)
        if durable_success_urls:
            cls._merge_all_turn_urls(chat_req, durable_success_urls)

        for source_url in durable_success_urls:
            indexed_entry = indexed_by_url.get(source_url, {})
            prepared_url = str(indexed_entry.get("prepared_url") or source_url).strip() or source_url
            raw_status = str(indexed_entry.get("status") or default_status).strip().lower() or default_status
            status = "indexed" if raw_status in {"indexed", "completed"} else raw_status

            vector_store_file_id = str(indexed_entry.get("vector_store_file_id") or "").strip() or None
            openai_file_id = str(
                indexed_entry.get("openai_file_id")
                or indexed_entry.get("source_openai_file_id")
                or successful_openai_file_ids_by_source.get(source_url)
                or reused_openai_file_ids_by_source.get(source_url)
                or ""
            ).strip() or None

            if not _is_durable_url(prepared_url):
                prepared_url = source_url

            VectorStoreManifestManager.upsert_entry(
                manifest,
                source_url=source_url,
                prepared_url=prepared_url,
                source="tool_add_files",
                representation=(
                    "native_vector_store" if prepared_url == source_url else "converted_txt_vector_store"
                ),
                status=status,
                turn=turn,
                last_error=None,
                vector_store_file_id=vector_store_file_id,
                openai_file_id=openai_file_id,
                last_indexed_turn=turn if status == "indexed" else None,
            )

        chat_req.vector_store_manifest = manifest

    @classmethod
    def _apply_vector_store_tool_state_update(
        cls,
        *,
        chat_req: OpenRouterChatReq,
        args: dict[str, Any],
        tool_output_str: str,
        step_index: int,
    ) -> None:
        payload = cls._extract_tool_payload_dict(tool_output_str)
        if not payload:
            return

        vector_store = payload.get("vector_store") if isinstance(payload.get("vector_store"), dict) else {}
        chat_req.vector_store_id = (
            payload.get("vector_store_id")
            or vector_store.get("id")
            or getattr(chat_req, "vector_store_id", None)
        )
        chat_req.vector_store_name = (
            payload.get("vector_store_name")
            or vector_store.get("name")
            or getattr(chat_req, "vector_store_name", None)
        )

        operation = str(args.get("operation") or "").strip() or str(payload.get("operation") or "").strip()

        if operation == "delete_vector_store":
            chat_req.vector_store_id = None
            chat_req.vector_store_name = None
            chat_req.vector_store_manifest = []
            chat_req.current_turn_vector_store_urls = []
            return

        if operation == "delete_vector_store_files":
            deleted_vector_store_file_ids = [
                str(x).strip()
                for x in (args.get("delete_file_ids") or [])
                if str(x).strip()
            ]
            cls._remove_vector_store_manifest_entries(
                chat_req=chat_req,
                vector_store_file_ids=deleted_vector_store_file_ids,
            )
            return

        if operation == "reindex_files":
            add_result = payload.get("add_result") if isinstance(payload.get("add_result"), dict) else {}
            deleted_vector_store_file_ids = [
                str(x).strip()
                for x in (args.get("delete_file_ids") or [])
                if str(x).strip()
            ]
            cls._remove_vector_store_manifest_entries(
                chat_req=chat_req,
                vector_store_file_ids=deleted_vector_store_file_ids,
            )
            if add_result:
                cls._apply_vector_store_add_payload_to_state(chat_req=chat_req, payload=add_result)
            return

        if operation == "add_files":
            cls._apply_vector_store_add_payload_to_state(chat_req=chat_req, payload=payload)
            return

    async def _apply_enable_mcp_tool_injection(
        self,
        *,
        ctx: Context,
        chat_req: OpenRouterChatReq,
        tool_output_str: str,
        user: UserORM | None,
        db: AsyncIOMotorDatabase | None,
    ) -> None:
        """Add the tools of a just-enabled MCP server to the in-flight request.

        `chat_req.tools` already holds resolved tool definitions by this point, so the
        MCP tools must be resolved to full definitions here; appending a bare server
        name would be forwarded verbatim to the API and rejected.
        """
        if user is None or db is None:
            return

        payload = self._extract_tool_payload_dict(tool_output_str)
        if not payload:
            return

        server_name = str(payload.get("server_name") or "").strip()
        if not payload.get("success") or not server_name:
            return

        if any(b.server_name == server_name for b in self.mcp_tool_bindings.values()):
            logger.bind(ctx=ctx, mcp_server_name=server_name).debug(
                "MCP tools already injected, skipping"
            )
            return

        mcp_doc = await UserMCP(user=user, db=db).get_mcp_by_server_name(
            ctx=ctx, mcp_server_name=server_name, decrypt=True
        )
        if not mcp_doc:
            logger.bind(ctx=ctx, mcp_server_name=server_name).warning(
                "enable_mcp reported success but MCP was not found"
            )
            return

        tool_params, bindings = await ToolFactoryOpenRouter.get_mcp_chat_completion_tool_param(
            ctx=ctx, mcp_docs=[mcp_doc]
        )
        if not tool_params:
            return

        existing_tools = list(chat_req.tools or [])
        existing_names = {
            name
            for tool in existing_tools
            if isinstance(tool, dict)
            and (name := (tool.get("function") or {}).get("name")) is not None
        }
        new_tools = [t for t in tool_params if t["function"]["name"] not in existing_names]
        if not new_tools:
            return

        chat_req.tools = existing_tools + [dict(t) for t in new_tools]
        self.register_mcp_tool_bindings(bindings)

        logger.bind(ctx=ctx, mcp_server_name=server_name).info(
            f"Injected {len(new_tools)} MCP tools from {server_name} into OpenRouter request"
        )

    @classmethod
    def _merge_all_turn_urls(cls, chat_req: OpenRouterChatReq, urls: list[str]) -> None:
        urls = dedupe_urls(urls)
        if not urls:
            return
        current_all = dedupe_urls(list(getattr(chat_req, "all_turn_urls", None) or []))
        merged = dedupe_urls(current_all + urls)
        chat_req.all_turn_urls = merged[-2000:]

        current_turn = dedupe_urls(list(getattr(chat_req, "current_turn_urls", None) or []))
        chat_req.current_turn_urls = dedupe_urls(current_turn + urls)[-2000:]


    @classmethod
    def _replace_tool_call_arguments_in_messages(
        cls,
        chat_req: OpenRouterChatReq,
        *,
        tool_call_id: str,
        tool_args: Any,
        tool_name: str | None = None,
        allow_legacy_function_call: bool = False,
    ) -> None:
        if not tool_call_id:
            return
        if chat_req is None:
            return
        args_str: str
        if isinstance(tool_args, str):
            args_str = tool_args
        else:
            try:
                args_str = json.dumps(tool_args, ensure_ascii=False, default=str)
            except Exception:
                args_str = str(tool_args)
        messages = list(getattr(chat_req, "messages", None) or [])
        
        for msg in reversed(messages):

            if not isinstance(msg, dict):
                continue

            if msg.get("role") != "assistant":
                continue

            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    if str(tc.get("id") or "") != str(tool_call_id):
                        continue
                    fn = tc.get("function")
                    if not isinstance(fn, dict):
                        fn = {}
                        tc["function"] = fn
                    fn["arguments"] = args_str
                    return

            if allow_legacy_function_call:
                function_call = msg.get("function_call")
                if isinstance(function_call, dict):
                    fc_name = str(function_call.get("name") or "").strip()
                    if not tool_name or fc_name == str(tool_name).strip():
                        function_call["arguments"] = args_str
                        return

    @classmethod
    def _get_available_turn_history(cls, chat_req: OpenRouterChatReq) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        pinned = cls._normalize_dict_list(getattr(chat_req, "pinned_messages", None) or [])
        history = cls._normalize_dict_list(getattr(chat_req, "turn_history", None) or [])
        return history, pinned

    @classmethod
    def _get_available_tool_results(cls, chat_req: OpenRouterChatReq) -> list[dict[str, Any]]:
        history = cls._normalize_dict_list(getattr(chat_req, "tool_results_history", None) or [])
        current = cls._normalize_dict_list(getattr(chat_req, "tool_results", None) or [])

        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for item in history + current:
            tool_call_id = str(item.get("tool_call_id") or "")
            if not tool_call_id or tool_call_id in seen:
                continue
            seen.add(tool_call_id)
            merged.append(item)
        return merged

    @staticmethod
    def _build_persisted_tool_result(
        *,
        chat_req: OpenRouterChatReq,
        tool_call_id: str,
        name: str,
        step_index: int,
        input_payload: dict[str, Any] | None,
        output_text: Any,
        is_error: bool,
        urls: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "tool_call_id": str(tool_call_id),
            "name": str(name),
            "chat_turn": int(getattr(chat_req, "chat_turn", 0) or 0),
            "step_index": int(step_index),
            "input": dict(input_payload or {}),
            "output_text": OpenRouterChat._truncate_tool_output_text(output_text),
            "is_error": bool(is_error),
            "urls": dedupe_urls([str(x) for x in (urls or []) if x]),
            "generated_files": [],
            "meta": dict(meta or {}),
        }

    @staticmethod
    def _merge_tool_results(chat_req: OpenRouterChatReq, new_items: list[dict[str, Any]]) -> None:
        existing = getattr(chat_req, "tool_results", None)
        if existing is None:
            return

        existing_items: list[dict[str, Any]] = OpenRouterChat._normalize_dict_list(existing)
        existing_ids = {str(x.get("tool_call_id") or "") for x in existing_items}

        merged: list[dict[str, Any]] = list(existing_items)
        for item in new_items:
            tool_call_id = str(item.get("tool_call_id") or "")
            if not tool_call_id or tool_call_id in existing_ids:
                continue
            existing_ids.add(tool_call_id)
            merged.append(item)

        chat_req.tool_results = merged[-2000:]

    @staticmethod
    async def get_model_based_config(ctx: Context, chat_req: OpenRouterChatReq) -> Dict[str, Any]:
        chat_req_dict = chat_req.to_openrouter_api_payload()
        structured_output = chat_req.get_structured_output_format()
        if structured_output:
            chat_req_dict.pop("response_format", None)
            chat_req_dict["response_format"] = structured_output

        for k in (
            "json_schema",
            "response_schema",
            "output_type",
        ):
            chat_req_dict.pop(k, None)

        chat_req_dict.pop("prompt_cache_key", None)
        chat_req_dict.pop("prompt_cache_retention", None)

        extra_body = dict(chat_req_dict.get("extra_body") or {})

        # OpenRouter context compression plugin (config-driven; default enabled).
        plugins: list[Any] = []
        existing_plugins = extra_body.get("plugins")
        if isinstance(existing_plugins, list):
            for plugin in existing_plugins:
                if isinstance(plugin, dict) and str(plugin.get("id") or "").strip() == "context-compression":
                    continue
                plugins.append(plugin)
        compression_plugin: dict[str, Any] = {"id": "context-compression"}
        if not bool(getattr(chat_req, "context_compression_enabled", True)):
            compression_plugin["enabled"] = False
        plugins.append(compression_plugin)
        extra_body["plugins"] = plugins

        # OpenRouter sticky session routing key (agentic continuity + cache hits).
        if getattr(chat_req, "session_id", None):
            extra_body["session_id"] = str(chat_req.session_id)
        else:
            extra_body.pop("session_id", None)

        # Reasoning tokens (OpenRouter extension). Default: exclude from responses and do not persist.
        reasoning_cfg = extra_body.get("reasoning") if isinstance(extra_body.get("reasoning"), dict) else {}
        if bool(getattr(chat_req, "reasoning_exclude", True)):
            reasoning_cfg["exclude"] = True
        else:
            reasoning_cfg.pop("exclude", None)
        if reasoning_cfg:
            extra_body["reasoning"] = reasoning_cfg
        else:
            extra_body.pop("reasoning", None)

        chat_req_dict["extra_body"] = extra_body

        # Never send reasoning fields back in subsequent requests (store-only).
        sanitized_messages: list[Any] = []
        for m in chat_req_dict.get("messages") or []:
            if isinstance(m, dict):
                m = strip_reasoning_fields(m)
            sanitized_messages.append(m)
        if sanitized_messages:
            chat_req_dict["messages"] = sanitized_messages

        logger.bind(
            ctx=ctx,
            code=AppCode.OPEN_ROUTER,
            model=str(chat_req.model),
            stream=bool(chat_req.stream),
            tools_count=len(chat_req.tools or []),
        ).debug("OpenRouter chat config prepared")
        return chat_req_dict
    
    @retry(
        retry=retry_if_not_exception_type((HTTPException, asyncio.CancelledError)),
        stop=stop_after_attempt(3),
        wait=wait_fixed(3),
        reraise=True,
    )
    async def chat(
        self,
        *,
        app_state: AppState,
        chat_req: OpenRouterChatReq,
        user: UserORM | None = None,
        ctx: Context | None = None,
        db: AsyncIOMotorDatabase | None = None,
        notify: Notify | None = None,
        tool_output_text_to_file_char_limit: int | None = None,
        tool_input_text_to_file_char_limit: int | None = None,
    ) -> ChatCompletion | None:
        if ctx is None:
            raise ValueError("ctx is required")


        if chat_req.stream:
            return await self.chat_loop_stream(
                ctx=ctx,
                app_state=app_state,
                chat_req=chat_req,
                user=user,
                db=db,
                notify=notify,
                tool_output_text_to_file_char_limit=tool_output_text_to_file_char_limit,
                tool_input_text_to_file_char_limit=tool_input_text_to_file_char_limit,
            )

        return await self.chat_loop(
            ctx=ctx,
            app_state=app_state,
            chat_req=chat_req,
            user=user,
            db=db,
            notify=notify,
            tool_output_text_to_file_char_limit=tool_output_text_to_file_char_limit,
            tool_input_text_to_file_char_limit=tool_input_text_to_file_char_limit,
        )

    async def chat_loop(
        self,
        *,
        ctx: Context,
        app_state: AppState,
        chat_req: OpenRouterChatReq,
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase | None = None,
        notify: Notify | None = None,
        tool_output_text_to_file_char_limit: int | None = None,
        tool_input_text_to_file_char_limit: int | None = None,
    ) -> ChatCompletion:
        max_loop = self._get_max_loop(chat_req)
        curr_loop = 0

        while True:
            app_state.raise_if_cancelled()
            snapshot_openrouter(ctx, app_state, chat_req, curr_loop)
            self._deliver_instructions(ctx, chat_req, await drain_instructions(app_state), notify)
            if curr_loop >= max_loop:
                return await self._finalize_after_max_loop(
                    ctx=ctx,
                    app_state=app_state,
                    chat_req=chat_req,
                    notify=notify,
                    max_loop=max_loop,
                )
            curr_loop += 1

            chat_completion_config = await OpenRouterChat.get_model_based_config(ctx=ctx, chat_req=chat_req)
            notify(ctx, "OpenRouter thinking ...") if notify else None

            chat_completion = await self.client.chat.completions.create(**chat_completion_config)
            TokenUsageService.record(
                ctx=ctx,
                surface=LlmSurface.openrouter,
                app_state=app_state,
                usage=getattr(chat_completion, "usage", None),
                model=str(getattr(chat_completion, "model", None) or chat_req.model),
                loop=curr_loop,
            )
            choice = chat_completion.choices[-1]

            logger.bind(
                ctx=ctx,
                code=AppCode.OPEN_ROUTER,
                loop=curr_loop,
                finish_reason=choice.finish_reason,
                choice=choice.model_dump(exclude_none=True),
            ).info("OpenRouter choice")

            # Persist assistant message (may include tool_calls)
            assistant_message = choice.message.model_dump(exclude_none=True)
            if bool(getattr(chat_req, "reasoning_exclude", True)) and isinstance(assistant_message, dict):
                assistant_message = strip_reasoning_fields(assistant_message)
            chat_req.messages.append(assistant_message)

            ran_tools = False
            if choice.message.tool_calls or choice.finish_reason == "tool_calls":
                if not choice.message.tool_calls:
                    raise HTTPException(
                        status_code=500,
                        detail="OpenRouter returned finish_reason=tool_calls but no tool_calls were provided",
                    )

                tool_messages = await self.run_tools(
                    ctx=ctx,
                    app_state=app_state,
                    chat_req=chat_req,
                    choice=choice,
                    step_index=curr_loop,
                    user=user,
                    db=db,
                    notify=notify,
                    tool_output_text_to_file_char_limit=tool_output_text_to_file_char_limit,
                    tool_input_text_to_file_char_limit=tool_input_text_to_file_char_limit,
                )
                chat_req.messages += tool_messages
                ran_tools = True

            elif getattr(choice.message, "function_call", None) is not None or choice.finish_reason == "function_call":
                function_call = getattr(choice.message, "function_call", None)
                if not function_call:
                    raise HTTPException(
                        status_code=500,
                        detail="OpenRouter returned finish_reason=function_call but no function_call was provided",
                    )

                fn_name, fn_args = OpenRouterChat._extract_function_call_parts(function_call)
                logger.bind(
                    ctx=ctx,
                    code=AppCode.OPEN_ROUTER,
                    loop=curr_loop,
                    finish_reason=choice.finish_reason,
                    function_call_name=fn_name,
                    function_call_args_preview=truncate_str(fn_args, limit=20),
                ).info("OpenRouter legacy function_call received")

                fn_msg, _persisted = await self._run_single_function_call(
                    ctx=ctx,
                    chat_req=chat_req,
                    function_call=function_call,
                    step_index=curr_loop,
                    user=user,
                    db=db,
                    notify=notify,
                    tool_output_text_to_file_char_limit=tool_output_text_to_file_char_limit,
                    tool_input_text_to_file_char_limit=tool_input_text_to_file_char_limit,
                )
                chat_req.messages.append(fn_msg)
                ran_tools = True

            if ran_tools:
                continue

            if choice.finish_reason == "length":
                message: ChatCompletionUserMessageParam = {"role": "user", "content": "Continue"}
                chat_req.messages += [message]
                continue

            # stop / content_filter / other
            return chat_completion

    async def chat_loop_stream(
        self,
        *,
        ctx: Context,
        app_state: AppState,
        chat_req: OpenRouterChatReq,
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase | None = None,
        notify: Notify | None = None,
        tool_output_text_to_file_char_limit: int | None = None,
        tool_input_text_to_file_char_limit: int | None = None,
    ) -> ChatCompletion:
        max_loop = self._get_max_loop(chat_req)
        curr_loop = 0

        notify_main = Notify(topic=text_topic(notify.topic), payload=NotifyPayload.snapshot) if notify else None
        notify_complimentary = Notify(topic=notify.topic) if notify else None

        while True:
            app_state.raise_if_cancelled()
            snapshot_openrouter(ctx, app_state, chat_req, curr_loop)
            self._deliver_instructions(ctx, chat_req, await drain_instructions(app_state), notify)
            if curr_loop >= max_loop:
                return await self._finalize_after_max_loop(
                    ctx=ctx,
                    app_state=app_state,
                    chat_req=chat_req,
                    notify=notify,
                    max_loop=max_loop,
                )
            curr_loop += 1

            builder = StreamingResponseBuilder(
                ctx=ctx,
                notify_main=notify_main,
                notify_complimentary=notify_complimentary,
            )

            chat_completion_config = await OpenRouterChat.get_model_based_config(ctx=ctx, chat_req=chat_req)
            chat_completion_config["stream"] = True

            notify(ctx, "OpenRouter streaming ...") if notify else None
            stream = await self.client.chat.completions.create(**chat_completion_config)
            assert isinstance(stream, AsyncStream)

            async for chunk in stream:
                builder.ingest(chunk)

            tool_calls_req = builder.tool_calls_req()
            if tool_calls_req and notify_complimentary:
                builder.finalize_tool_calls()

            function_call_req = builder.function_call_req()

            content_value = builder.content_value()
            refusal_value = builder.refusal_value()
            content_text = content_value if isinstance(content_value, str) else ("" if content_value is None else str(content_value))
            refusal_text = refusal_value if isinstance(refusal_value, str) else ("" if refusal_value is None else str(refusal_value))
            fc_name = None if not function_call_req else str(function_call_req.get("name") or "")

            logger.bind(
                ctx=ctx,
                app_code=AppCode.OPEN_ROUTER,
                stream=True,
                loop=curr_loop,
                completion_id=builder.id_completion,
                model=builder.model,
                finish_reason=builder.finish_reason,
                content_len=len(content_text),
                refusal_len=len(refusal_text),
                tool_calls_count=len(tool_calls_req),
                function_call_name=fc_name or None,
            ).debug("OpenRouter stream snapshot")

            if OpenRouterChat._is_effectively_empty_stream_completion(
                content=content_value,
                refusal=refusal_value,
                tool_calls_count=len(tool_calls_req),
                has_function_call=bool(function_call_req),
            ):
                logger.bind(
                    ctx=ctx,
                    app_code=AppCode.OPEN_ROUTER,
                    stream=True,
                    loop=curr_loop,
                    completion_id=builder.id_completion,
                    model=builder.model,
                    finish_reason=builder.finish_reason,
                    tool_calls_count=len(tool_calls_req),
                    function_call_present=bool(function_call_req),
                ).warning("OpenRouter returned an empty streamed completion")
                raise HTTPException(
                    status_code=502,
                    detail="Empty streamed completion from OpenRouter (no content, tool calls, function_call, or refusal)",
                )

            chat_completion_obj = builder.build_completion()
            TokenUsageService.record(
                ctx=ctx,
                surface=LlmSurface.openrouter,
                app_state=app_state,
                usage=getattr(chat_completion_obj, "usage", None),
                model=str(getattr(chat_completion_obj, "model", None) or chat_req.model),
                loop=curr_loop,
            )
            chat_completion_message = builder.message

            ran_tools = False
            if tool_calls_req or builder.finish_reason == "tool_calls":
                if not tool_calls_req:
                    raise HTTPException(
                        status_code=500,
                        detail="OpenRouter returned finish_reason=tool_calls but no tool_calls were provided",
                    )

                assistant_tool_msg = {
                    "role": "assistant",
                    "content": None if chat_completion_message is None else chat_completion_message.get("content"),
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": tc["arguments"]},
                        }
                        for tc in tool_calls_req
                    ],
                }
                chat_req.messages.append(assistant_tool_msg)

                tool_messages = await self.run_tools_stream(
                    ctx=ctx,
                    app_state=app_state,
                    chat_req=chat_req,
                    tool_calls=tool_calls_req,
                    step_index=curr_loop,
                    user=user,
                    db=db,
                    notify=notify,
                    tool_output_text_to_file_char_limit=tool_output_text_to_file_char_limit,
                    tool_input_text_to_file_char_limit=tool_input_text_to_file_char_limit,
                )
                chat_req.messages += tool_messages
                ran_tools = True

            elif function_call_req is not None or builder.finish_reason == "function_call":
                if not function_call_req:
                    raise HTTPException(
                        status_code=500,
                        detail="OpenRouter returned finish_reason=function_call but no function_call was provided",
                    )

                assistant_fc_msg = {
                    "role": "assistant",
                    "content": None if chat_completion_message is None else chat_completion_message.get("content"),
                    "function_call": {
                        "name": str(function_call_req.get("name") or ""),
                        "arguments": str(function_call_req.get("arguments") or ""),
                    },
                }
                chat_req.messages.append(assistant_fc_msg)

                fn_msg, _persisted = await self._run_single_function_call(
                    ctx=ctx,
                    app_state=app_state,
                    chat_req=chat_req,
                    function_call=function_call_req,
                    step_index=curr_loop,
                    user=user,
                    db=db,
                    notify=notify,
                    tool_output_text_to_file_char_limit=tool_output_text_to_file_char_limit,
                    tool_input_text_to_file_char_limit=tool_input_text_to_file_char_limit,
                )
                chat_req.messages.append(fn_msg)
                ran_tools = True

            else:
                if chat_completion_message:
                    msg = chat_completion_message
                    if bool(getattr(chat_req, "reasoning_exclude", True)) and isinstance(msg, dict):
                        msg = strip_reasoning_fields(msg)
                    chat_req.messages.append(msg)

            if ran_tools:
                continue

            if builder.finish_reason == "length":
                message: ChatCompletionUserMessageParam = {"role": "user", "content": "Continue"}
                chat_req.messages += [message]
                continue

            return chat_completion_obj

    async def _run_single_function_call(
        self,
        *,
        ctx: Context,
        app_state: AppState,
        chat_req: OpenRouterChatReq,
        function_call: Any,
        step_index: int,
        user: UserORM | None,
        db: AsyncIOMotorDatabase | None,
        notify: Notify | None,
        tool_output_text_to_file_char_limit: int | None,
        tool_input_text_to_file_char_limit: int | None,
    ) -> tuple[ChatCompletionFunctionMessageParam, dict[str, Any]]:
        tool_name, tool_args = OpenRouterChat._extract_function_call_parts(function_call)
        if not tool_name:
            raise HTTPException(status_code=500, detail="OpenRouter returned function_call without a name")

        logger.bind(
            ctx=ctx,
            app_code=AppCode.OPEN_ROUTER,
            step_index=step_index,
            function_call_name=tool_name,
            function_call_args_preview=truncate_str(tool_args, limit=50),
        ).info("Executing legacy function_call as tool")
        tool_call_id = OpenRouterChat._new_legacy_tool_call_id(step_index=step_index, tool_name=tool_name)
        tool_msg, persisted = await self._run_single_tool(
            ctx=ctx,
            app_state=app_state,
            chat_req=chat_req,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_args=tool_args,
            step_index=step_index,
            user=user,
            db=db,
            notify=notify,
            tool_output_text_to_file_char_limit=tool_output_text_to_file_char_limit,
            tool_input_text_to_file_char_limit=tool_input_text_to_file_char_limit,
        )
        fn_msg = ChatCompletionFunctionMessageParam(
            role="function",
            name=tool_name,
            content=tool_msg.get("content"),
        )
        return fn_msg, persisted


    async def _run_single_tool(
        self,
        *,
        ctx: Context,
        app_state: AppState,
        chat_req: OpenRouterChatReq,
        tool_call_id: str,
        tool_name: str,
        tool_args: Any,
        step_index: int,
        user: UserORM | None,
        db: AsyncIOMotorDatabase | None,
        notify: Notify | None,
        tool_output_text_to_file_char_limit: int | None,
        tool_input_text_to_file_char_limit: int | None,
    ) -> tuple[ChatCompletionToolMessageParam, dict[str, Any]]:
        call = ToolCallContext(
            ctx=ctx,
            app_state=app_state,
            chat_req=chat_req,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_args=tool_args,
            step_index=step_index,
            user=user,
            db=db,
            notify=notify,
            mcp_tool_bindings=self.mcp_tool_bindings,
            tool_output_text_to_file_char_limit=tool_output_text_to_file_char_limit,
            tool_input_text_to_file_char_limit=tool_input_text_to_file_char_limit,
        )
        outcome = await self.tool_dispatcher.dispatch(call)
        return outcome.tool_msg, outcome.persisted

    async def run_tools(
        self,
        *,
        ctx: Context,
        app_state: AppState,
        chat_req: OpenRouterChatReq,
        choice: Any,
        step_index: int,
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase | None = None,
        notify: Notify | None = None,
        tool_output_text_to_file_char_limit: int | None = None,
        tool_input_text_to_file_char_limit: int | None = None,
    ) -> List[ChatCompletionMessageParam]:
        messages: list[ChatCompletionMessageParam] = []

        for tool_call in choice.message.tool_calls:
            tool_call_id = tool_call.id
            tool_name = tool_call.function.name
            tool_args = tool_call.function.arguments

            tool_msg, _persisted = await self._run_single_tool(
                ctx=ctx,
                app_state=app_state,
                chat_req=chat_req,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                tool_args=tool_args,
                step_index=step_index,
                user=user,
                db=db,
                notify=notify,
                tool_output_text_to_file_char_limit=tool_output_text_to_file_char_limit,
                tool_input_text_to_file_char_limit=tool_input_text_to_file_char_limit,
            )
            messages.append(tool_msg)

        return messages

    async def run_tools_stream(
        self,
        *,
        ctx: Context,
        app_state: AppState,
        chat_req: OpenRouterChatReq,
        tool_calls: list[dict[str, Any]],
        step_index: int,
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase | None = None,
        notify: Notify | None = None,
        tool_output_text_to_file_char_limit: int | None = None,
        tool_input_text_to_file_char_limit: int | None = None,
    ) -> List[ChatCompletionMessageParam]:
        messages: list[ChatCompletionMessageParam] = []

        for tool_call in tool_calls:
            tool_call_id = str(tool_call.get("id") or "")
            tool_name = str(tool_call.get("name") or "")
            tool_args = tool_call.get("arguments")

            tool_msg, _persisted = await self._run_single_tool(
                ctx=ctx,
                app_state=app_state,
                chat_req=chat_req,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                tool_args=tool_args,
                step_index=step_index,
                user=user,
                db=db,
                notify=notify,
                tool_output_text_to_file_char_limit=tool_output_text_to_file_char_limit,
                tool_input_text_to_file_char_limit=tool_input_text_to_file_char_limit,
            )
            messages.append(tool_msg)

        return messages
