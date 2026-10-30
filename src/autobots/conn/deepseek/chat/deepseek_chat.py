import json
from typing import Any, Dict, List, Optional, Set

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.deepseek.chat.deepseek_common import (
    DeepSeekChatError,
    DeepSeekCommon,
    _safe_notify,
)
from src.autobots.conn.deepseek.chat.deepseek_models import (
    DeepSeekChatRequest,
    DeepSeekChatResult,
    DeepSeekMessage,
    DeepSeekRole,
    DeepSeekToolCall,
)
from src.autobots.conn.deepseek.chat.deepseek_tool_executor import DeepSeekToolExecutor, ToolPolicy
from src.autobots.conn.deepseek.chat.deepseek_trace import RunTrace
from src.autobots.core.cancellation.run_instructions import RunInstruction, drain_instructions
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.chat_snapshot.provider_snapshots import snapshot_deepseek
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.token_usage.token_usage_doc_model import LlmSurface
from src.autobots.token_usage.token_usage_service import TokenUsageService
from src.autobots.user.user_orm_model import UserORM

HARD_STOP_CODES = {"REPEAT_CALL_DETECTED", "MAX_TOTAL_TOOL_CALLS", "MAX_CALLS_PER_TOOL", "SIDE_EFFECTS_DISABLED"}


_FINALIZE_SYSTEM = (
    "You are finalizing the assistant response.\n"
    "Tools are disabled. Do not request tool calls.\n"
    "Use the provided tool results to answer as best as possible.\n"
    "If information is missing, ask at most ONE concise follow-up question.\n"
)


_TOOL_SYSTEM_GUIDE = """[AUTOBOTS_TOOLING_GUIDE]
You may call tools when needed, but you must avoid loops and repeated calls.

Tool results are returned as a JSON envelope:
{ ok, tool, text, urls, file_ids, error, meta }

Rules:
- You must have a good reason for using a specific tool.
- Do NOT repeat the same tool call with the same arguments.
- If a tool returns an error_code like REPEAT_CALL_DETECTED / MAX_TOTAL_TOOL_CALLS / MAX_CALLS_PER_TOOL, stop calling tools and finalize with what you have.
- If a side-effect tool is needed, ask for user confirmation (confirm=true) or propose a safe alternative.
- If tools are unavailable or blocked, produce the best possible answer from existing tool results.
"""

def _model_name(req: DeepSeekChatRequest, resp: Any = None) -> str:
    return str(getattr(resp, "model", None) or getattr(req.model, "value", req.model))


def _json_load_maybe(s: Any) -> Dict[str, Any]:
    if s is None:
        return {}
    if isinstance(s, dict):
        return s
    if isinstance(s, str):
        ss = s.strip()
        if not ss:
            return {}
        try:
            v = json.loads(ss)
            return v if isinstance(v, dict) else {"__raw": v}
        except Exception:
            return {"__raw": ss}
    return {"__raw": s}


class DeepSeekChat:
    def __init__(self, *, common: DeepSeekCommon) -> None:
        self.common = common


    @staticmethod
    def _deliver_instructions(
        ctx: Context,
        req: DeepSeekChatRequest,
        instructions: List[RunInstruction],
        notify: Optional[Notify],
    ) -> None:
        for instruction in instructions:
            req.messages.append(
                DeepSeekMessage(role=DeepSeekRole.user, content=instruction.as_user_message())
            )
            logger.bind(ctx=ctx).info(f"Run instruction applied: {instruction.text}")
            _safe_notify(notify, ctx, f"Instruction applied: {instruction.text}")

    def _inject_tool_system_guide(self, req: DeepSeekChatRequest) -> None:
        for m in req.messages:
            if m.role == DeepSeekRole.system and "[AUTOBOTS_TOOLING_GUIDE]" in (m.content or ""):
                return
        # insert at start
        req.messages = [DeepSeekMessage(role=DeepSeekRole.system, content=_TOOL_SYSTEM_GUIDE)] + list(req.messages or [])


    def _extract_tool_calls(self, resp: Any, custom_names: Set[str]) -> List[DeepSeekToolCall]:
        """
        Convert OpenAI tool_calls to DeepSeekToolCall pydantic objects (filtered by allowlist , it's necessary).
        """
        out: List[DeepSeekToolCall] = []
        try:
            choices = getattr(resp, "choices", None) or []
            if not choices:
                return out
            msg = getattr(choices[0], "message", None)
            tcs = getattr(msg, "tool_calls", None) or []
            for tc in tcs:
                fn = getattr(tc, "function", None)
                name = getattr(fn, "name", None) if fn else None
                if not name or name not in custom_names:
                    continue
                out.append(
                    DeepSeekToolCall(
                        id=str(getattr(tc, "id", "")),
                        type="function",
                        function={
                            "name": str(name),
                            "arguments": getattr(fn, "arguments", "{}"),
                        },
                    )
                )
        except Exception:
            pass
        return out

    async def _finalize_without_tools(
        self,
        *,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        req: DeepSeekChatRequest,
        trace: RunTrace,
        notify: Optional[Notify],
        loop: int,
    ) -> DeepSeekChatResult:
        
        # Build a final request with tools disabled
        final_req = req.model_copy(deep=True)
        final_req.stream = False
        final_req.tools = []
        final_req.tool_choice = "none"

        # Add a system instruction and compact tool results
        lines: List[str] = []
        for r in trace.rounds:
            for tr in r.tool_results:
                lines.append(
                    f"- [round {r.round_no}] tool={tr.get('tool')} ok={tr.get('ok')} error_code={tr.get('error_code')}\n"
                    f"  output_preview: {tr.get('output_preview')}\n"
                )

        tool_block = "\n".join(lines) if lines else "(no tool results)"

        final_req.messages = list(final_req.messages or [])
        final_req.messages.append(DeepSeekMessage(role=DeepSeekRole.system, content=_FINALIZE_SYSTEM))
        final_req.messages.append(
            DeepSeekMessage(
                role=DeepSeekRole.user,
                content=f"TOOL RESULTS (COMPACT):\n{tool_block}\n\nNow provide the best final answer.",
            )
        )

        resp = await self.common.sample(req=final_req)
        TokenUsageService.record(
            ctx=ctx,
            surface=LlmSurface.deepseek,
            app_state=app_state,
            usage=getattr(resp, "usage", None),
            model=_model_name(final_req, resp),
            loop=loop,
        )
        return self.common.to_result(resp)

    async def run(
        self,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        req: DeepSeekChatRequest,
        *,
        db: AsyncIOMotorDatabase,
        notify: Optional[Notify] = None,
        tool_runner: Optional[Any] = None,
        max_rounds: int = 10,
        # policies
        default_policy: Optional[ToolPolicy] = None,
        policy_overrides: Optional[Dict[str, ToolPolicy]] = None,
        allowed_tools: Optional[Set[str]] = None,
    ) -> DeepSeekChatResult:
        if db is None:
            raise DeepSeekChatError("db is required (AsyncIOMotorDatabase)")
        
        self._inject_tool_system_guide(req)
        trace = RunTrace()

        tool_names = allowed_tools or {t.function.get("name") for t in (req.tools or []) if isinstance(t.function, dict)}
        tool_names = {t for t in tool_names if t}

        executor = None
        if tool_names:
            if tool_runner is None:
                raise DeepSeekChatError("tools are enabled but no tool_runner provided")
            executor = DeepSeekToolExecutor(
                tool_runner=tool_runner,
                default_policy=default_policy or ToolPolicy(),
                policy_overrides=policy_overrides or {},
            )

        rounds = 0
        while rounds < max_rounds:
            app_state.raise_if_cancelled()
            snapshot_deepseek(ctx, app_state, req, rounds)
            self._deliver_instructions(ctx, req, await drain_instructions(app_state), notify)
            rounds += 1
            rr = trace.ensure_round(rounds)
            _safe_notify(notify, ctx, f"DeepSeek thinking... (Round {rounds})")

            # Use streaming only when user asked for it AND we are not expecting tool loops.
            # If tools are enabled, we still can stream, but it complicates; we stream and collect tool_calls.
            if req.stream:
                text, reasoning, tool_calls, resp, finish_reason = await self.common.stream_text(
                    ctx=ctx, req=req, notify=notify
                )
                TokenUsageService.record(
                    ctx=ctx,
                    surface=LlmSurface.deepseek,
                    app_state=app_state,
                    usage=getattr(resp, "usage", None),
                    model=_model_name(req, resp),
                    loop=rounds,
                )
                rr.assistant_text = text
                rr.reasoning_preview = (reasoning[:500] + "...") if reasoning and len(reasoning) > 300 else reasoning

                if tool_calls:
                    # Convert tool_calls to allowlist only
                    filtered = []
                    for tc in tool_calls:
                        fn = (tc.get("function") or {})
                        if fn.get("name") in tool_names:
                            filtered.append(tc)
                    if not filtered:
                        trace.set_stop(kind="MODEL_DONE", round_no=rounds)
                        trace.finish()
                        return self.common.stream_to_result(text=text, reasoning=reasoning, tool_calls=[], last_event=resp)

                    # raise exception tool executor is not initialized
                    if not executor:
                        raise DeepSeekChatError("tool calls returned but tool executor not initialized")

                    # Append assistant tool_call message to history
                    req.messages.append(
                        DeepSeekMessage(
                            role=DeepSeekRole.assistant,
                            content=text or "",
                            reasoning_content=("" if req.strip_reasoning_in_history else (reasoning or "")),
                            tool_calls=[
                                DeepSeekToolCall(
                                    id=str(tc.get("id") or ""),
                                    type="function",
                                    function={
                                        "name": str((tc.get("function") or {}).get("name") or ""),
                                        "arguments": (tc.get("function") or {}).get("arguments") or "{}",
                                    },
                                )
                                for tc in filtered
                            ],
                        )
                    )

                    # Execute each tool call and append tool messages
                    for tc in filtered:
                        t_id = str(tc.get("id") or "")
                        fn = tc.get("function") or {}
                        tname = str(fn.get("name") or "")
                        args = _json_load_maybe(fn.get("arguments"))

                        rr.tool_calls.append({"tool": tname, "raw_args": fn.get("arguments"), "parsed_args": args, "id": t_id})

                        ok, out_text, err_code = await executor.execute_tool_call(
                            ctx=ctx, app_state=app_state, tool_name=tname, args=args, notify=notify
                        )

                        rr.tool_results.append(
                            {"tool": tname, "ok": ok, "output_preview": (out_text[:2500] + "...") if len(out_text) > 2500 else out_text, "error_code": err_code}
                        )

                        # Hard stops - finalize without tools
                        if err_code in HARD_STOP_CODES:
                            trace.total_tool_calls = executor.state.total_calls
                            trace.per_tool_calls = dict(executor.state.per_tool_calls)
                            trace.set_stop(kind=err_code, detail=out_text, round_no=rounds, tool=tname, error_code=err_code)
                            trace.finish()
                            return await self._finalize_without_tools(ctx=ctx, app_state=app_state, user=user, db=db, req=req, trace=trace, notify=notify, loop=rounds + 1)

                        req.messages.append(
                            DeepSeekMessage(role=DeepSeekRole.tool, tool_call_id=t_id, content=out_text)
                        )

                    # continue to next round
                    continue

                # no tool calls => done
                trace.set_stop(kind="MODEL_DONE", round_no=rounds)
                trace.finish()
                return self.common.stream_to_result(text=text, reasoning=reasoning, tool_calls=[], last_event=resp)

            # Non-streaming
            resp = await self.common.sample(req=req)
            TokenUsageService.record(
                ctx=ctx,
                surface=LlmSurface.deepseek,
                app_state=app_state,
                usage=getattr(resp, "usage", None),
                model=_model_name(req, resp),
                loop=rounds,
            )
            result = self.common.to_result(resp)

            rr.assistant_text = result.content
            rr.reasoning_preview = (result.reasoning_content[:300] + "...") if result.reasoning_content and len(result.reasoning_content) > 300 else result.reasoning_content

            # tool loop
            if result.tool_calls:
                if not executor:
                    # tools returned but we aren't configured
                    trace.set_stop(kind="TOOLS_RETURNED_BUT_DISABLED", detail="Model requested tools but executor missing", round_no=rounds)
                    trace.finish()
                    return result

                # filter allowlist
                filtered = [tc for tc in result.tool_calls if (tc.get("function") or {}).get("name") in tool_names]
                if not filtered:
                    trace.set_stop(kind="MODEL_DONE", round_no=rounds)
                    trace.finish()
                    return result

                # Append assistant tool_call message
                req.messages.append(
                    DeepSeekMessage(
                        role=DeepSeekRole.assistant,
                        content=result.content or "",
                        tool_calls=[
                            DeepSeekToolCall(
                                id=str(tc.get("id") or ""),
                                type="function",
                                function={
                                    "name": str((tc.get("function") or {}).get("name") or ""),
                                    "arguments": (tc.get("function") or {}).get("arguments") or "{}",
                                },
                            )
                            for tc in filtered
                        ],
                    )
                )

                # Execute tools
                for tc in filtered:
                    t_id = str(tc.get("id") or "")
                    fn = tc.get("function") or {}
                    tname = str(fn.get("name") or "")
                    args = _json_load_maybe(fn.get("arguments"))

                    rr.tool_calls.append({"tool": tname, "raw_args": fn.get("arguments"), "parsed_args": args, "id": t_id})

                    ok, out_text, err_code = await executor.execute_tool_call(
                        ctx=ctx, app_state=app_state, tool_name=tname, args=args, notify=notify
                    )

                    rr.tool_results.append(
                        {"tool": tname, "ok": ok, "output_preview": (out_text[:2500] + "...") if len(out_text) > 2500 else out_text, "error_code": err_code}
                    )

                    if err_code in HARD_STOP_CODES:
                        trace.total_tool_calls = executor.state.total_calls
                        trace.per_tool_calls = dict(executor.state.per_tool_calls)
                        trace.set_stop(kind=err_code, detail=out_text, round_no=rounds, tool=tname, error_code=err_code)
                        trace.finish()
                        return await self._finalize_without_tools(ctx=ctx, app_state=app_state, user=user, db=db, req=req, trace=trace, notify=notify, loop=rounds + 1)

                    req.messages.append(DeepSeekMessage(role=DeepSeekRole.tool, tool_call_id=t_id, content=out_text))

                # continue loop
                continue

            # no tool calls - done
            trace.set_stop(kind="MODEL_DONE", round_no=rounds)
            trace.finish()
            return result

        # max rounds - finalize
        if executor:
            trace.total_tool_calls = executor.state.total_calls
            trace.per_tool_calls = dict(executor.state.per_tool_calls)

        trace.set_stop(kind="MAX_ROUNDS", detail=f"Reached max_rounds={max_rounds}", round_no=max_rounds)
        trace.finish()
        return await self._finalize_without_tools(ctx=ctx, app_state=app_state, user=user, db=db, req=req, trace=trace, notify=notify, loop=rounds + 1)
