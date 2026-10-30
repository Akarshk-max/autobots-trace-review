import time
from typing import Any, Dict, List, Optional, Set, Tuple

import xai_sdk
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from xai_sdk.chat import system as xai_system
from xai_sdk.chat import tool_result
from xai_sdk.chat import user as xai_user

from src.autobots.conn.grok.grok_chat.grok_common import GrokChatError, GrokCommon, _safe_notify
from src.autobots.conn.grok.grok_chat.grok_models import (
    GrokChatRequest,
    GrokChatResult,
    GrokMessage,
    GrokRole,
)
from src.autobots.conn.grok.grok_chat.grok_tool_executor import GrokToolExecutor, ToolPolicy
from src.autobots.conn.grok.grok_chat.grok_trace import RunTrace
from src.autobots.conn.grok.grok_chat.grok_user_config import CompiledPlan
from src.autobots.core.cancellation.run_instructions import RunInstruction, drain_instructions
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.chat_snapshot.provider_snapshots import snapshot_grok
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.token_usage.token_usage_service import LlmSurface, TokenUsageService
from src.autobots.user.user_orm_model import UserORM

try:
    from xai_sdk.tools import get_tool_call_type
except Exception:
    get_tool_call_type = None


_TOOL_SYSTEM_GUIDE = """[AUTOBOTS_TOOLING_GUIDE]
You may call tools when needed, but you must avoid loops and repeated calls.

Tool results are returned as a JSON envelope:
{ ok, tool, text, urls, file_ids, error, meta }

Rules:
- Do NOT repeat the same tool call with the same arguments.
- If a tool returns an error_code like REPEAT_CALL_DETECTED / MAX_TOTAL_TOOL_CALLS / MAX_CALLS_PER_TOOL, stop calling tools and finalize with what you have.
- If a side-effect tool is needed, ask for user confirmation (confirm=true) or propose a safe alternative.
- If tools are unavailable or blocked, produce the best possible answer from existing tool results.
"""


_FINALIZE_SYSTEM = """You are finalizing the assistant response.
Tools are disabled. You must not request tool calls.
Use the provided Tool Results to answer as best as possible.
If information is missing, clearly state what is unknown and ask at most one concise follow-up question.
Keep the answer structured and helpful.
"""


HARD_STOP_CODES = {
    "REPEAT_CALL_DETECTED",
    "MAX_TOTAL_TOOL_CALLS",
    "MAX_CALLS_PER_TOOL",
    "SIDE_EFFECTS_DISABLED",
}


class GrokChat:
    def __init__(
        self,
        *,
        client: xai_sdk.AsyncClient,
        timeout_sec: float = 420.0,
        model_registry=None,
    ) -> None:
        self.client = client
        self.common = GrokCommon(model_registry=model_registry) if model_registry else GrokCommon()
        self.timeout_sec = timeout_sec

    @staticmethod
    def _deliver_instructions(
        ctx: Context,
        chat: Any,
        req: GrokChatRequest,
        instructions: List[RunInstruction],
        notify: Optional[Notify],
    ) -> None:
        """Append run instructions to the live chat and to the request messages."""
        for instruction in instructions:
            text = instruction.as_user_message()
            chat.append(xai_user(text))
            req.messages = list(req.messages or []) + [GrokMessage(role=GrokRole.user, content=text)]
            logger.bind(ctx=ctx).info(f"Run instruction applied: {instruction.text}")
            _safe_notify(notify, ctx, f"Instruction applied: {instruction.text}")

    async def _instructions_at_exit(
        self,
        ctx: Context,
        app_state: AppState,
        chat: Any,
        req: GrokChatRequest,
        notify: Optional[Notify],
    ) -> bool:
        pending = await drain_instructions(app_state)
        if not pending:
            return False
        self._deliver_instructions(ctx, chat, req, pending, notify)
        return True

    def _inject_tool_system_guide(self, req: GrokChatRequest) -> None:
        # avoid duplication
        for m in req.messages:
            if m.role == GrokRole.system and "[AUTOBOTS_TOOLING_GUIDE]" in (m.content or ""):
                return
        # insert at start
        req.messages = [GrokMessage(role=GrokRole.system, content=_TOOL_SYSTEM_GUIDE)] + list(
            req.messages or []
        )

    def _last_user_goal(self, req: GrokChatRequest) -> str:
        for m in reversed(req.messages or []):
            if m.role == GrokRole.user:
                return (m.content or "").strip()
        return ""

    def _extract_custom_calls(self, resp: Any, custom_names: Set[str]) -> List[Any]:
        tcalls = getattr(resp, "tool_calls", None) or []
        custom_calls = []
        for tc in tcalls:
            if get_tool_call_type is not None:
                try:
                    if get_tool_call_type(tc) != "client_side_tool":
                        continue
                except Exception:
                    pass
            fn = getattr(getattr(tc, "function", None), "name", None)
            if fn and fn in custom_names:
                custom_calls.append(tc)
        return custom_calls

    def _should_finalize_early(self, trace: RunTrace) -> Optional[Tuple[str, str]]:
        """
        Returns (kind, detail) if we should stop early and finalize.
        """
        if not trace.rounds:
            return None

        # Timeout storm: 3 timeouts in last 5 tool results
        last_results = []
        for r in reversed(trace.rounds):
            last_results.extend(list(reversed(r.tool_results)))
            if len(last_results) >= 5:
                break
        last_results = last_results[:5]
        if sum(1 for x in last_results if (x.error_code == "TOOL_TIMEOUT")) >= 3:
            return ("TIMEOUT_STORM", "3 timeouts in recent tool calls")

        # No-progress via info-gain hash: unchanged for 3 rounds
        if len(trace.progress_hashes) >= 3:
            h1, h2, h3 = trace.progress_hashes[-1], trace.progress_hashes[-2], trace.progress_hashes[-3]
            if h1 and (h1 == h2 == h3):
                return ("NO_PROGRESS", "No new information gained for 3 rounds")

        # Fallback: 3 rounds without any ok tool results
        if len(trace.rounds) >= 3:
            last3 = trace.rounds[-3:]
            if all(not any(tr.ok for tr in rr.tool_results) for rr in last3):
                return ("NO_PROGRESS", "No successful tool results for 3 rounds")

        return None

    def _build_finalization_prompt(self, req: GrokChatRequest, trace: RunTrace) -> str:
        goal = self._last_user_goal(req)
        stop = trace.stop_reason.model_dump() if trace.stop_reason else {"kind": "MODEL_DONE"}

        # compact tool results
        lines: List[str] = []
        for r in trace.rounds:
            for tr in r.tool_results:
                urls = ", ".join(tr.urls[:3]) if tr.urls else ""
                fids = ", ".join(tr.file_ids[:3]) if tr.file_ids else ""
                err = tr.error_code or tr.error or ""
                rkey = ""
                try:
                    rkey = str((tr.meta or {}).get("result_key") or "")
                except Exception:
                    rkey = ""
                lines.append(
                    f"- [round {r.round_no}] tool={tr.tool} ok={tr.ok} error_code={err} cache_hit={tr.cache_hit} result_key={rkey}\n"
                    f"  preview: {tr.text_preview}\n"
                    f"  urls: {urls}\n"
                    f"  file_ids: {fids}\n"
                )

        tool_block = "\n".join(lines) if lines else "(no tool results)"

        return (
            f"USER GOAL:\n{goal}\n\n"
            f"RUN_ID: {trace.run_id}\n\n"
            f"TOOL RESULTS (COMPACT):\n{tool_block}\n\n"
            f"STOP REASON:\n{json_dump_safe(stop)}\n\n"
            "INSTRUCTIONS:\n"
            "- Produce the best final answer using the tool results above.\n"
            "- Prefer referencing result_key when helpful instead of repeating raw content.\n"
            "- If there are conflicts, explain them.\n"
            "- If something is missing, ask at most ONE follow-up question.\n"
        )

    async def _finalize(
        self,
        *,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        req: GrokChatRequest,
        notify: Optional[Notify],
        model_name: str,
        trace: RunTrace,
    ) -> GrokChatResult:
        # tools disabled, search disabled
        final_req = req.model_copy(deep=True)
        final_req.stream = False
        final_req.include = []
        final_req.live_search.enabled = False
        final_req.server_tools.web_search = False
        final_req.server_tools.x_search = False
        final_req.server_tools.code_execution = False

        chat = await self.common.build_chat(
            client=self.client,
            req=final_req,
            tools=None,
            search_parameters=None,
        )

        chat.append(xai_system(_FINALIZE_SYSTEM))
        chat.append(xai_user(self._build_finalization_prompt(req, trace)))

        resp = await self.common.sample(chat=chat, req=final_req)
        TokenUsageService.record(
            ctx=ctx,
            surface=LlmSurface.grok,
            app_state=app_state,
            usage=getattr(resp, "usage", None),
            model=model_name,
            loop=len(trace.rounds) + 1,
        )
        text = (getattr(resp, "content", None) or "").strip()
        return self.common.to_result(resp, text)

    async def _execute_tools_parallel(
        self,
        *,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Optional[Notify],
        executor: GrokToolExecutor,
        tool_calls: List[Any],
        round_no: int,
        max_parallel: int,
        chat_state: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        sem = __import__("asyncio").Semaphore(max_parallel)

        async def _one(tc: Any) -> str:
            async with sem:
                fn = tc.function.name
                raw_args = getattr(tc.function, "arguments", None)
                if fn == "grok_chat_history_query":
                    return await self._run_chat_history_query_tool(
                        ctx=ctx,
                        user=user,
                        db=db,
                        raw_args=raw_args,
                        notify=notify,
                        round_no=round_no,
                        chat_state=chat_state or {},
                    )
                return await executor.execute_one(
                    ctx=ctx,
                    app_state=app_state,
                    tool_name=fn,
                    raw_args=raw_args,
                    notify=notify,
                    round_no=round_no,
                )

        return list(await __import__("asyncio").gather(*[_one(tc) for tc in tool_calls]))

    async def _run_chat_history_query_tool(
        self,
        *,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        raw_args: Any,
        notify: Optional[Notify],
        round_no: int,
        chat_state: Dict[str, Any],
    ) -> str:
        import json as _json

        from src.autobots.conn.grok.grok_chat.grok_tool_executor import build_envelope
        from src.autobots.conn.grok.grok_chat.grok_tools_defination import normalize_tool_args
        from src.autobots.llm.tools.action_chat_history_query_grok import (
            ActionChatHistoryQueryGrok,
            GrokHistoryQueryReq,
        )

        args = normalize_tool_args(raw_args)
        if not isinstance(args, dict):
            args = {}

        try:
            query_req = GrokHistoryQueryReq.model_validate(args)
        except Exception as exc:
            return build_envelope(
                ok=False,
                tool="grok_chat_history_query",
                text="Invalid tool arguments. Fix the schema issues and retry ONCE; otherwise proceed without this tool.",
                error=str(exc),
                error_code="SCHEMA_INVALID",
                meta={"round": round_no},
            )

        semantic_query = str(getattr(query_req, "semantic_query", None) or "").strip()
        history_vector_store_id = chat_state.get("history_vector_store_id")

        if semantic_query:
            if not history_vector_store_id:
                query_payload = {
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
                return build_envelope(
                    ok=True,
                    tool="grok_chat_history_query",
                    text=_json.dumps(query_payload, ensure_ascii=False),
                    meta={"round": round_no},
                )

            # due to dependecy graph resoulution, it can cause circular import error so let it be placed here.
            from src.autobots.llm.tools.openai_vector_store_tool import (
                OpenAIVectorStoreToolConfig,
                OpenAIVectorStoreToolOperation,
                openai_vector_store_tool,
            )

            try:
                vs_result = await openai_vector_store_tool(
                    config=OpenAIVectorStoreToolConfig(
                        operation=OpenAIVectorStoreToolOperation.QUERY_VECTOR_STORE,
                        vector_store_id=history_vector_store_id,
                        query=semantic_query,
                        max_num_results=query_req.semantic_max_results,
                    ),
                    ctx=ctx,
                    user=user,
                    db=db,
                    notify=notify,
                )
                if isinstance(vs_result, Exception):
                    raise vs_result

                vs_payload: Dict[str, Any] = {}
                if vs_result.blocks and vs_result.blocks[0].text:
                    vs_payload = _json.loads(vs_result.blocks[0].text)

                vs_results = vs_payload.get("results") or []
                query_payload = {
                    "total_count": len(vs_results),
                    "returned_count": len(vs_results),
                    "results": vs_results,
                    "summary": {
                        "mode": "semantic",
                        "semantic_query": semantic_query,
                        "history_vector_store_id": history_vector_store_id,
                    },
                }
                return build_envelope(
                    ok=True,
                    tool="grok_chat_history_query",
                    text=_json.dumps(query_payload, ensure_ascii=False),
                    meta={"round": round_no},
                )
            except Exception as exc:
                logger.bind(ctx=ctx).exception(
                    "Failed executing grok_chat_history_query semantic search: {}", exc
                )
                return build_envelope(
                    ok=False,
                    tool="grok_chat_history_query",
                    text="Semantic search failed. Fall back to turn/text_query filtering.",
                    error=str(exc),
                    error_code="TOOL_EXECUTION_FAILED",
                    meta={"round": round_no},
                )

        # Exact/structured mode
        try:
            query_req.set_turn_history(
                turn_history=chat_state.get("turn_history") or [],
                pinned_messages=chat_state.get("pinned_messages") or [],
            )
            query_res = ActionChatHistoryQueryGrok._execute_query(query_req)
            return build_envelope(
                ok=True,
                tool="grok_chat_history_query",
                text=query_res.model_dump_json(exclude_none=True),
                meta={"round": round_no},
            )
        except Exception as exc:
            logger.bind(ctx=ctx).exception(
                "Failed executing grok_chat_history_query exact search: {}", exc
            )
            return build_envelope(
                ok=False,
                tool="grok_chat_history_query",
                text="Chat history query failed.",
                error=str(exc),
                error_code="TOOL_EXECUTION_FAILED",
                meta={"round": round_no},
            )

    async def run(
        self,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        req: GrokChatRequest,
        *,
        db: AsyncIOMotorDatabase,
        notify: Optional[Notify] = None,
        tool_runner: Optional[Any] = None,
        max_rounds: int = 10,
        plan: Optional[CompiledPlan] = None,
        chat_state: Optional[Dict[str, Any]] = None,
    ) -> GrokChatResult:
        if db is None:
            raise GrokChatError("db is required (AsyncIOMotorDatabase)")

        # Always inject tooling behavior
        self._inject_tool_system_guide(req)

        # Attach convenience files/images to last user message
        await self.common._inject_convenience_attachments(req)

        # Live search SearchParameters
        sp = None
        if req.live_search.enabled:
            sp = self.common._build_search_parameters(req.live_search.parameters)

        # Build custom tool objects for Grok
        server_tool_objs = self.common.build_server_tool_objects(req.server_tools)
        custom_tool_objs = self.common.build_custom_tool_objects(req.custom_tools)
        tools = (
            (server_tool_objs + custom_tool_objs)
            if (server_tool_objs or custom_tool_objs)
            else None
        )

        model_name = self.common.model_registry.resolve(req.model)
        logger.bind(ctx=ctx, user=user.id).info(f"[GROK CHAT] Model selected is {model_name}")

        chat = await self.common.build_chat(
            client=self.client,
            req=req,
            tools=tools,
            search_parameters=sp,
        )

        await self.common.append_messages(
            ctx=ctx,
            user_=user,
            db=db,
            client=self.client,
            chat=chat,
            messages=req.messages,
            notify=notify,
            model_name=model_name,
        )

        # Custom tool allowlist
        custom_names: Set[str] = {t.name for t in (req.custom_tools or [])}

        # policies
        if plan is not None:
            default_policy = plan.policies.default
            overrides = plan.policies.overrides
            max_parallel_tools = int(getattr(plan, "max_parallel_tools", 4) or 4)
        else:
            default_policy = ToolPolicy(
                timeout_sec=1200.0,
                max_arg_bytes=50_000,
                max_output_chars_for_model=45_000,
                max_calls_per_chat=10,
                max_total_tool_calls=30,
                max_repeat_same_call=2,
                redact_secrets=True,
            )
            overrides = {
                "search_web": ToolPolicy(
                    timeout_sec=100.0, max_calls_per_chat=10, max_total_tool_calls=10
                ),
                "read_url": ToolPolicy(
                    timeout_sec=120.0, max_calls_per_chat=10, max_total_tool_calls=10
                ),
            }
            max_parallel_tools = 4

        executor = GrokToolExecutor(
            specs=req.custom_tools or [],
            tool_runner=tool_runner,
            default_policy=default_policy,
            policy_overrides=overrides,
        )

        trace = RunTrace()
        t_run0 = time.perf_counter()

        # when stream variable is set True
        # it will use notify to send chunk data with concatenation at each time with previous chunk
        # so that at last it would be final result
        if req.stream:
            rounds = 0
            while rounds < max_rounds:
                app_state.raise_if_cancelled()
                snapshot_grok(ctx, app_state, req, rounds)
                self._deliver_instructions(ctx, chat, req, await drain_instructions(app_state), notify)
                rounds += 1
                _safe_notify(notify, ctx, f"Grok thinking... (Round {rounds})")

                t_model0 = time.perf_counter()
                text, resp = await self.common.stream_text(ctx=ctx, chat=chat, notify=notify)
                TokenUsageService.record(
                    ctx=ctx,
                    surface=LlmSurface.grok,
                    app_state=app_state,
                    usage=getattr(resp, "usage", None),
                    model=model_name,
                    loop=rounds,
                )
                model_ms = int((time.perf_counter() - t_model0) * 1000)

                rr = trace.ensure_round(rounds)
                rr.assistant_text = (text or "").strip()
                rr.meta = getattr(rr, "meta", {})  # allow extra
                rr.meta["model_ms"] = model_ms

                custom_calls = self._extract_custom_calls(resp, custom_names)

                # No tool calls: finalize if we used tools earlier, else return directly
                if not custom_calls:
                    if await self._instructions_at_exit(ctx, app_state, chat, req, notify):
                        continue
                    trace.total_tool_calls = executor.state.total_calls
                    trace.per_tool_calls = dict(executor.state.per_tool_calls)
                    trace.set_stop(kind="MODEL_DONE", round_no=rounds)
                    trace.metrics["run_id"] = trace.run_id
                    trace.metrics["total_ms"] = int((time.perf_counter() - t_run0) * 1000)
                    trace.finish()

                    if any(r.tool_results for r in trace.rounds):
                        return await self._finalize(
                            ctx=ctx,
                            app_state=app_state,
                            user=user,
                            db=db,
                            req=req,
                            notify=notify,
                            model_name=model_name,
                            trace=trace,
                        )
                    return self.common.to_result(resp, rr.assistant_text)

                if tool_runner is None:
                    raise GrokChatError("custom tools requested but no tool_runner provided")

                # Execute tool calls
                for tc in custom_calls:
                    fn = tc.function.name
                    raw_args = getattr(tc.function, "arguments", None)
                    rr.tool_calls.append({"tool": fn, "raw_args": raw_args, "norm_args": {}, "fp": None})

                # execute in parallel
                t_tools0 = time.perf_counter()
                payloads = await self._execute_tools_parallel(
                    ctx=ctx,
                    app_state=app_state,
                    user=user,
                    db=db,
                    notify=notify,
                    executor=executor,
                    tool_calls=custom_calls,
                    round_no=rounds,
                    max_parallel=max_parallel_tools,
                    chat_state=chat_state,
                )
                tools_ms = int((time.perf_counter() - t_tools0) * 1000)
                rr.meta["tools_ms"] = tools_ms

                # append tool results in original order
                for tc, payload in zip(custom_calls, payloads):
                    fn = tc.function.name
                    chat.append(tool_result(payload))

                    tr = RunTrace.envelope_to_record(payload)
                    rr.tool_results.append(tr)

                    # hard-stop codes - finalize now
                    if tr.error_code in HARD_STOP_CODES:
                        trace.total_tool_calls = executor.state.total_calls
                        trace.per_tool_calls = dict(executor.state.per_tool_calls)
                        trace.set_stop(
                            kind=tr.error_code or "HARD_STOP",
                            detail=tr.error or "",
                            round_no=rounds,
                            tool=fn,
                            error_code=tr.error_code,
                        )
                        trace.metrics["run_id"] = trace.run_id
                        trace.metrics["total_ms"] = int((time.perf_counter() - t_run0) * 1000)
                        trace.finish()
                        return await self._finalize(
                            ctx=ctx,
                            app_state=app_state,
                            user=user,
                            db=db,
                            req=req,
                            notify=notify,
                            model_name=model_name,
                            trace=trace,
                        )

                # progress hash for no-progress detection
                trace.add_progress_hash(RunTrace.compute_progress_hash(rr))

                early = self._should_finalize_early(trace)
                if early:
                    trace.total_tool_calls = executor.state.total_calls
                    trace.per_tool_calls = dict(executor.state.per_tool_calls)
                    trace.set_stop(kind=early[0], detail=early[1], round_no=rounds)
                    trace.metrics["run_id"] = trace.run_id
                    trace.metrics["total_ms"] = int((time.perf_counter() - t_run0) * 1000)
                    trace.finish()
                    return await self._finalize(
                        ctx=ctx,
                        app_state=app_state,
                        user=user,
                        db=db,
                        req=req,
                        notify=notify,
                        model_name=model_name,
                        trace=trace,
                    )

            # max rounds - finalize (not dead-end) llm has option to opt for more reasoning always in this case if any other tools are available suitable to use
            trace.total_tool_calls = executor.state.total_calls
            trace.per_tool_calls = dict(executor.state.per_tool_calls)
            trace.set_stop(kind="MAX_ROUNDS", detail=f"Reached max_rounds={max_rounds}", round_no=max_rounds)
            trace.metrics["run_id"] = trace.run_id
            trace.metrics["total_ms"] = int((time.perf_counter() - t_run0) * 1000)
            trace.finish()
            return await self._finalize(
                ctx=ctx,
                app_state=app_state,
                user=user,
                db=db,
                req=req,
                notify=notify,
                model_name=model_name,
                trace=trace,
            )

        # when stream is set to be False
        rounds = 0
        while rounds < max_rounds:
            app_state.raise_if_cancelled()
            snapshot_grok(ctx, app_state, req, rounds)
            self._deliver_instructions(ctx, chat, req, await drain_instructions(app_state), notify)
            rounds += 1
            _safe_notify(notify, ctx, f"Grok thinking... (Round {rounds})")

            t_model0 = time.perf_counter()
            resp = await self.common.sample(chat=chat, req=req)
            TokenUsageService.record(
                ctx=ctx,
                surface=LlmSurface.grok,
                app_state=app_state,
                usage=getattr(resp, "usage", None),
                model=model_name,
                loop=rounds,
            )
            model_ms = int((time.perf_counter() - t_model0) * 1000)

            text = (getattr(resp, "content", None) or "").strip()

            rr = trace.ensure_round(rounds)
            rr.assistant_text = text
            rr.meta = getattr(rr, "meta", {})
            rr.meta["model_ms"] = model_ms

            custom_calls = self._extract_custom_calls(resp, custom_names)

            if not custom_calls:
                if await self._instructions_at_exit(ctx, app_state, chat, req, notify):
                    continue
                trace.total_tool_calls = executor.state.total_calls
                trace.per_tool_calls = dict(executor.state.per_tool_calls)
                trace.set_stop(kind="MODEL_DONE", round_no=rounds)
                trace.metrics["run_id"] = trace.run_id
                trace.metrics["total_ms"] = int((time.perf_counter() - t_run0) * 1000)
                trace.finish()

                if any(r.tool_results for r in trace.rounds):
                    return await self._finalize(
                        ctx=ctx,
                        app_state=app_state,
                        user=user,
                        db=db,
                        req=req,
                        notify=notify,
                        model_name=model_name,
                        trace=trace,
                    )
                return self.common.to_result(resp, text)

            if tool_runner is None:
                raise GrokChatError("custom tools requested but no tool_runner provided")

            for tc in custom_calls:
                fn = tc.function.name
                raw_args = getattr(tc.function, "arguments", None)
                rr.tool_calls.append({"tool": fn, "raw_args": raw_args, "norm_args": {}, "fp": None})

            t_tools0 = time.perf_counter()
            payloads = await self._execute_tools_parallel(
                ctx=ctx,
                app_state=app_state,
                user=user,
                db=db,
                notify=notify,
                executor=executor,
                tool_calls=custom_calls,
                round_no=rounds,
                max_parallel=max_parallel_tools,
                chat_state=chat_state,
            )
            rr.meta["tools_ms"] = int((time.perf_counter() - t_tools0) * 1000)

            for tc, payload in zip(custom_calls, payloads):
                fn = tc.function.name
                chat.append(tool_result(payload))

                tr = RunTrace.envelope_to_record(payload)
                rr.tool_results.append(tr)

                if tr.error_code in HARD_STOP_CODES:
                    trace.total_tool_calls = executor.state.total_calls
                    trace.per_tool_calls = dict(executor.state.per_tool_calls)
                    trace.set_stop(
                        kind=tr.error_code or "HARD_STOP",
                        detail=tr.error or "",
                        round_no=rounds,
                        tool=fn,
                        error_code=tr.error_code,
                    )
                    trace.metrics["run_id"] = trace.run_id
                    trace.metrics["total_ms"] = int((time.perf_counter() - t_run0) * 1000)
                    trace.finish()
                    return await self._finalize(
                        ctx=ctx,
                        app_state=app_state,
                        user=user,
                        db=db,
                        req=req,
                        notify=notify,
                        model_name=model_name,
                        trace=trace,
                    )

            trace.add_progress_hash(RunTrace.compute_progress_hash(rr))

            early = self._should_finalize_early(trace)
            if early:
                trace.total_tool_calls = executor.state.total_calls
                trace.per_tool_calls = dict(executor.state.per_tool_calls)
                trace.set_stop(kind=early[0], detail=early[1], round_no=rounds)
                trace.metrics["run_id"] = trace.run_id
                trace.metrics["total_ms"] = int((time.perf_counter() - t_run0) * 1000)
                trace.finish()
                return await self._finalize(
                    ctx=ctx,
                    app_state=app_state,
                    user=user,
                    db=db,
                    req=req,
                    notify=notify,
                    model_name=model_name,
                    trace=trace,
                )

        trace.total_tool_calls = executor.state.total_calls
        trace.per_tool_calls = dict(executor.state.per_tool_calls)
        trace.set_stop(kind="MAX_ROUNDS", detail=f"Reached max_rounds={max_rounds}", round_no=max_rounds)
        trace.metrics["run_id"] = trace.run_id
        trace.metrics["total_ms"] = int((time.perf_counter() - t_run0) * 1000)
        trace.finish()
        return await self._finalize(
            ctx=ctx,
            app_state=app_state,
            user=user,
            db=db,
            req=req,
            notify=notify,
            model_name=model_name,
            trace=trace,
        )


def json_dump_safe(obj: Any) -> str:
    import json

    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return str(obj)