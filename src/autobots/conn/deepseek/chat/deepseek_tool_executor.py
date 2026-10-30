import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState


class ToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_sec: float = 1200.0
    max_arg_bytes: int = 50_000
    max_output_chars_for_model: int = 15_000

    max_calls_per_chat: int = 10
    max_total_tool_calls: int = 30
    max_repeat_same_call: int = 2

    side_effects: bool = False
    allow_side_effects: bool = False
    require_confirm_field: Optional[str] = None

    redact_secrets: bool = True

    cache_enabled: bool = False
    cache_ttl_sec: Optional[int] = None
    cache_max_entries: int = 32
    cache_max_total_bytes: int = 512_000
    cache_max_entry_bytes: int = 64_000


class ToolExecState(BaseModel):
    model_config = ConfigDict(extra="allow")

    total_calls: int = 0
    per_tool_calls: Dict[str, int] = Field(default_factory=dict)
    call_fingerprints: Dict[str, int] = Field(default_factory=dict)

    def bump_tool(self, tool_name: str) -> None:
        self.total_calls += 1
        self.per_tool_calls[tool_name] = self.per_tool_calls.get(tool_name, 0) + 1

    def bump_fp(self, fp: str) -> int:
        self.call_fingerprints[fp] = self.call_fingerprints.get(fp, 0) + 1
        return self.call_fingerprints[fp]


_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)\b\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)\b(bearer)\s+([a-z0-9\-\._=]+)"),
]


def redact_text(s: str) -> str:
    if not s:
        return s
    out = s
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: f"{m.group(1)}: ***REDACTED***", out)
    return out


def stable_json_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return json.dumps({"__repr__": repr(obj)}, ensure_ascii=False)


def sanitize_args_for_fp(args: Dict[str, Any]) -> Dict[str, Any]:
    drop = {"confirm", "nonce", "timestamp", "ts", "request_id"}
    out: Dict[str, Any] = {}
    for k, v in (args or {}).items():
        if not isinstance(k, str):
            continue
        if k in drop or k.startswith("_"):
            continue
        out[k] = v
    return out


def fingerprint(tool_name: str, args: Dict[str, Any]) -> str:
    payload = tool_name + "|" + stable_json_dumps(args)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _CacheEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    payload: str
    created_ms: int
    last_access_ms: int
    size_bytes: int
    ttl_sec: Optional[int] = None

    def expired(self, now_ms: int) -> bool:
        if self.ttl_sec is None:
            return False
        return (now_ms - self.created_ms) > int(self.ttl_sec * 1000)


class ToolCache:
    def __init__(self) -> None:
        self._store: "OrderedDict[str, _CacheEntry]" = OrderedDict()
        self.total_bytes: int = 0

    def get(self, key: str, now_ms: int) -> Optional[_CacheEntry]:
        ent = self._store.get(key)
        if not ent:
            return None
        if ent.expired(now_ms):
            self.delete(key)
            return None
        ent.last_access_ms = now_ms
        self._store.move_to_end(key)
        return ent

    def delete(self, key: str) -> None:
        ent = self._store.pop(key, None)
        if ent:
            self.total_bytes = max(0, self.total_bytes - int(ent.size_bytes))

    def put(self, key: str, entry: _CacheEntry, *, max_entries: int, max_total_bytes: int) -> None:
        if key in self._store:
            self.delete(key)
        self._store[key] = entry
        self._store.move_to_end(key)
        self.total_bytes += int(entry.size_bytes)

        while len(self._store) > max_entries or self.total_bytes > max_total_bytes:
            k, ent = self._store.popitem(last=False)
            self.total_bytes = max(0, self.total_bytes - int(ent.size_bytes))


class DeepSeekToolExecutor:
    """
    Executes your local tools (via tool_runner.run_tool) with:
      - schema/size guards
      - repeat protection
      - side-effect gating
      - optional caching
    """

    def __init__(
        self,
        *,
        tool_runner: Any,
        default_policy: ToolPolicy,
        policy_overrides: Optional[Dict[str, ToolPolicy]] = None,
    ) -> None:
        self.tool_runner = tool_runner
        self.default_policy = default_policy
        self.policy_overrides = policy_overrides or {}
        self.state = ToolExecState()
        self.cache = ToolCache()

    def policy_for(self, tool_name: str) -> ToolPolicy:
        p = self.policy_overrides.get(tool_name)
        if p:
            return p

        base = self.default_policy.model_copy(deep=True)

        # heuristic side-effect detection
        if tool_name.startswith(("create_", "update_", "delete_", "run_", "save_", "upload_", "send_")):
            base.side_effects = True
            base.require_confirm_field = base.require_confirm_field or "confirm"

        return base

    async def execute_tool_call(
        self,
        *,
        ctx: Context,
        app_state: AppState,
        tool_name: str,
        args: Dict[str, Any],
        notify: Optional[Notify],
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Returns (ok, tool_output_text, error_code)
        """
        policy = self.policy_for(tool_name)

        # global budgets
        if self.state.total_calls >= policy.max_total_tool_calls:
            return False, "Tool budget exceeded. Finalize using existing context.", "MAX_TOTAL_TOOL_CALLS"

        # arg size
        arg_bytes = len(stable_json_dumps(args).encode("utf-8"))
        if arg_bytes > policy.max_arg_bytes:
            return False, "Tool arguments too large. Reduce input size.", "ARGS_TOO_LARGE"

        # side effects gating
        if policy.side_effects and not policy.allow_side_effects:
            return False, "Side-effect tool disabled. Ask user for confirmation.", "SIDE_EFFECTS_DISABLED"

        if policy.side_effects and policy.require_confirm_field:
            cf = policy.require_confirm_field
            if args.get(cf) is not True:
                return False, f"Confirmation required: set '{cf}: true' to run this tool.", "CONFIRMATION_REQUIRED"

        # per-tool limit
        tcount = self.state.per_tool_calls.get(tool_name, 0)
        if tcount >= policy.max_calls_per_chat:
            return False, "Per-tool call limit reached. Finalize using existing context.", "MAX_CALLS_PER_TOOL"

        # repeat fingerprint
        fp = fingerprint(tool_name, sanitize_args_for_fp(args))
        rep = self.state.bump_fp(fp)
        if rep > policy.max_repeat_same_call:
            return False, "Repeated identical tool call detected. Stop tools and finalize.", "REPEAT_CALL_DETECTED"

        # cache
        if policy.cache_enabled and (not policy.side_effects):
            now_ms = int(time.time() * 1000)
            ent = self.cache.get(fp, now_ms)
            if ent:
                return True, ent.payload, None

        # execute
        self.state.bump_tool(tool_name)
        try:
            if notify:
                try:
                    notify(ctx, f"Using tool: {tool_name}")
                except Exception:
                    pass

            t0 = time.perf_counter()
            coro = self.tool_runner.run_tool(ctx,  app_state, tool_name, args, notify)
            out = await asyncio.wait_for(coro, timeout=policy.timeout_sec)
            dt_ms = int((time.perf_counter() - t0) * 1000)

            text = getattr(out, "text", None)
            if text is None:
                text = stable_json_dumps(out)

            text = str(text)
            if policy.redact_secrets:
                text = redact_text(text)

            if len(text) > policy.max_output_chars_for_model:
                text = text[: policy.max_output_chars_for_model] + "\n\n...[TRUNCATED]"

            logger.bind(tool=tool_name, duration_ms=dt_ms).info("Tool executed")

            # store cache
            if policy.cache_enabled and (not policy.side_effects):
                now_ms = int(time.time() * 1000)
                b = len(text.encode("utf-8"))
                if b <= policy.cache_max_entry_bytes:
                    self.cache.put(
                        fp,
                        _CacheEntry(
                            payload=text,
                            created_ms=now_ms,
                            last_access_ms=now_ms,
                            size_bytes=b,
                            ttl_sec=policy.cache_ttl_sec,
                        ),
                        max_entries=policy.cache_max_entries,
                        max_total_bytes=policy.cache_max_total_bytes,
                    )

            return True, text, None

        except asyncio.TimeoutError:
            return False, f"Tool timed out after {policy.timeout_sec}s.", "TOOL_TIMEOUT"
        except Exception as e:
            logger.bind(tool=tool_name).exception("Tool execution failed")
            return False, f"Tool execution failed: {e}", "TOOL_EXECUTION_FAILED"
