import asyncio
import random
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from src.autobots.conn.deepseek.chat.deepseek_models import (
    DeepSeekChatRequest,
    DeepSeekChatResult,
    DeepSeekMessage,
    DeepSeekUsage,
)
from src.autobots.conn.deepseek.deepseek import DeepSeekConnection
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify


class DeepSeekChatError(RuntimeError):
    pass


def _safe_notify(notify: Optional[Notify], ctx: Context, msg: str) -> None:
    if not notify:
        return
    try:
        notify(ctx, msg)
    except Exception:
        pass


def _coerce_usage(resp: Any) -> Optional[DeepSeekUsage]:
    try:
        u = getattr(resp, "usage", None)
        if not u:
            return None
        return DeepSeekUsage(
            prompt_tokens=int(getattr(u, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(u, "completion_tokens", 0) or 0),
            total_tokens=int(getattr(u, "total_tokens", 0) or 0),
            reasoning_tokens=int(getattr(u, "reasoning_tokens", 0) or 0),
        )
    except Exception:
        return None


def _extract_message(resp: Any) -> Any:
    # OpenAI-style: resp.choices[0].message
    choices = getattr(resp, "choices", None) or []
    if not choices:
        return None
    return getattr(choices[0], "message", None)


def _extract_finish_reason(resp: Any) -> Optional[str]:
    choices = getattr(resp, "choices", None) or []
    if not choices:
        return None
    return getattr(choices[0], "finish_reason", None)


def _coerce_tool_calls(msg: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        tcs = getattr(msg, "tool_calls", None) or []
        for tc in tcs:
            fn = getattr(tc, "function", None)
            out.append(
                {
                    "id": getattr(tc, "id", None),
                    "type": getattr(tc, "type", None),
                    "function": {
                        "name": getattr(fn, "name", None) if fn else None,
                        "arguments": getattr(fn, "arguments", None) if fn else None,
                    },
                }
            )
    except Exception:
        pass
    return out


def _coerce_reasoning_content(msg: Any) -> Optional[str]:
    # DeepSeek thinking mode may return reasoning_content
    try:
        rc = getattr(msg, "reasoning_content", None)
        return str(rc) if rc else None
    except Exception:
        return None


def _inject_attachments_into_text(messages: List[DeepSeekMessage]) -> List[DeepSeekMessage]:
    """
    Since DeepSeek doesn't support file/image attachments in chat,
    we are going to just inject attachment references into the message content.
    So that it can be consumed by the tool further
    """
    out: List[DeepSeekMessage] = []
    for m in messages:
        if not m.attachments:
            out.append(m)
            continue

        lines = [m.content or ""]
        lines.append("\n\n[ATTACHMENTS]")
        for a in m.attachments:
            label = a.filename or a.value
            lines.append(f"- ({a.kind}) {label}: {a.value}")
        nm = m.model_copy(deep=True)
        nm.content = "\n".join(lines).strip()
        nm.attachments = []
        out.append(nm)
    return out


class DeepSeekCommon:
    def __init__(self, conn: DeepSeekConnection) -> None:
        self.conn = conn

    def _build_openai_payload(self, req: DeepSeekChatRequest) -> Tuple[Dict[str, Any], bool]:
        """
        Returns (payload, use_beta_client).
        Use beta if any tool.strict is True (DeepSeek strict-mode is beta) :contentReference[oaicite:4]{index=4}
        Thinking mode is passed via extra_body in OpenAI SDK :contentReference[oaicite:5]{index=5}
        """
        msgs = _inject_attachments_into_text(req.messages or [])
        thinking_enabled = bool(getattr(req.thinking, "type", None) == "enabled")

        payload_msgs = [
            m.to_openai_dict(strip_reasoning=bool(req.strip_reasoning_in_history) , thinking_enabled=thinking_enabled)
            for m in msgs
        ]

        tools = [t.model_dump() for t in (req.tools or [])] if req.tools else None
        use_beta = bool(any(getattr(t, "strict", False) for t in (req.tools or [])))

        payload: Dict[str, Any] = {
            "model": req.model.value if hasattr(req.model, "value") else str(req.model),
            "messages": payload_msgs,
        }

        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.max_tokens is not None:
            payload["max_tokens"] = req.max_tokens
        if req.top_p is not None:
            payload["top_p"] = req.top_p
        if req.frequency_penalty is not None:
            payload["frequency_penalty"] = req.frequency_penalty
        if req.presence_penalty is not None:
            payload["presence_penalty"] = req.presence_penalty
        if req.stop is not None:
            payload["stop"] = req.stop

        if tools:
            payload["tools"] = tools
        if req.tool_choice is not None:
            payload["tool_choice"] = req.tool_choice

        if req.response_format is not None:
            payload["response_format"] = req.response_format.model_dump()

        if req.thinking and req.thinking.type:
            payload["extra_body"] = {"thinking": req.thinking.model_dump()}

        if req.stream:
            payload["stream"] = True

        return payload, use_beta

    async def _call_with_retry(
        self,
        *,
        payload: Dict[str, Any],
        beta: bool,
        timeout_sec: float,
        max_attempts: int = 4,
        base_backoff: float = 0.6,
        jitter: float = 0.25,
    ) -> Any:
        last_err: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                client = self.conn.client(beta=beta)
                return await asyncio.wait_for(client.chat.completions.create(**payload), timeout=timeout_sec)
            except Exception as e:
                last_err = e
                # best-effort transient classification
                msg = str(e).lower()
                transient = any(x in msg for x in ["timeout", "timed out", "429", "rate", "503", "502", "connection", "reset"])
                if attempt >= max_attempts or not transient:
                    raise
                sleep_s = base_backoff * (2 ** (attempt - 1)) + random.random() * jitter
                logger.warning("DeepSeek call failed (attempt {}/{}). Retrying in {:.2f}s. err={}", attempt, max_attempts, sleep_s, e)
                await asyncio.sleep(sleep_s)

        raise DeepSeekChatError(f"DeepSeek call failed after retries: {last_err}")

    async def sample(self, *, req: DeepSeekChatRequest) -> Any:
        payload, beta = self._build_openai_payload(req)
        return await self._call_with_retry(payload=payload, beta=beta, timeout_sec=float(req.timeout_sec))

    async def stream_text(
        self,
        *,
        ctx: Context,
        req: DeepSeekChatRequest,
        notify: Optional[Notify],
    ) -> Tuple[str, Optional[str], List[Dict[str, Any]], Any, Optional[str]]:
        """
        Streams until completion, aggregates:
          - content text
          - reasoning_content (if any)
          - tool_calls (if any)
          - finish_reason
        """
        payload, beta = self._build_openai_payload(req)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}

        client = self.conn.client(beta=beta)

        chunks: List[str] = []
        reasoning_chunks: List[str] = []
        tool_calls_acc: Dict[tuple, Dict[str, Any]] = {}
        last_resp = None
        finish_reason: Optional[str] = None

        stream = await client.chat.completions.create(**payload)

        async for event in stream:
            last_resp = event
            # event.choices[0].delta
            choices = getattr(event, "choices", None) or []
            if not choices:
                continue
            ch0 = choices[0]
            finish_reason = getattr(ch0, "finish_reason", None) or finish_reason
            delta = getattr(ch0, "delta", None)
            if not delta:
                continue

            txt = getattr(delta, "content", None)
            if txt:
                chunks.append(txt)
                _safe_notify(notify, ctx, "".join(chunks))

            rc = getattr(delta, "reasoning_content", None)
            if rc:
                reasoning_chunks.append(str(rc))

            # tool_calls streaming: delta.tool_calls[*].function.arguments can arrive in pieces
            tcs = getattr(delta, "tool_calls", None) or []
            for tc in tcs:
                idx = getattr(tc, "index", 0)
                tcid = getattr(tc, "id", None)
                tctype = getattr(tc, "type", "function")
                fn = getattr(tc, "function", None)
                fname = getattr(fn, "name", None) if fn else None
                fargs = getattr(fn, "arguments", None) if fn else None

                # Use index as the stable identity across streaming deltas.
                key = int(idx)

                cur = tool_calls_acc.get(key)
                if not cur:
                    cur = {
                        "id": None,
                        "type": "function",
                        "function": {"name": None, "arguments": ""},
                    }
                if tcid is not None:
                    cur["id"] = tcid
                if tctype is not None:
                    cur["type"] = tctype

                if fname is not None:
                    cur["function"]["name"] = fname

                if fargs is not None:
                    cur["function"]["arguments"] = (cur["function"].get("arguments") or "") + str(fargs)

                tool_calls_acc[key] = cur

        final_text = "".join(chunks).strip()
        final_reasoning = "".join(reasoning_chunks).strip() or None
        tool_calls = list(tool_calls_acc.values())

        return final_text, final_reasoning, tool_calls, last_resp, finish_reason

    def to_result(self, resp: Any) -> DeepSeekChatResult:
        msg = _extract_message(resp)
        text = (getattr(msg, "content", None) or "").strip() if msg else ""
        model = str(getattr(resp, "model", "") or "")
        tool_calls = _coerce_tool_calls(msg) if msg else []
        reasoning_content = _coerce_reasoning_content(msg) if msg else None

        return DeepSeekChatResult(
            content=text,
            model=model,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            usage=_coerce_usage(resp),
            raw={},
        )
    
    def stream_to_result(self, *, text: str, reasoning: Optional[str], tool_calls: List[Dict[str, Any]], last_event: Any) -> DeepSeekChatResult:
        model = ""
        try:
            model = str(getattr(last_event, "model", "") or "")
        except Exception:
            pass
        return DeepSeekChatResult(
            content=(text or "").strip(),
            model=model,
            tool_calls=tool_calls or [],
            reasoning_content=(reasoning or "").strip() or None,
            usage=None,
            raw={},
        )
