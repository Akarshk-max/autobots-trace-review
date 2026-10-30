import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from src.autobots.conn.grok.grok_chat.grok_common import GrokChatError, _safe_notify
from src.autobots.conn.grok.grok_chat.grok_models import CustomToolSpec
from src.autobots.conn.grok.grok_chat.grok_tools_defination import normalize_tool_args
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify

try:
    import jsonschema
except Exception:
    jsonschema = None
from src.autobots.data_model.app_state import AppState

# Policies / State
class ToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Safety and stability
    timeout_sec: float = 1200.0
    max_arg_bytes: int = 50_000

    # make this smaller for stable context growth
    max_output_chars_for_model: int = 65_000

    # Loop control
    max_calls_per_chat: int = 10  # per tool name
    max_total_tool_calls: int = 30  # across all tools
    max_repeat_same_call: int = 2  # same (tool,args) allowed N times

    # Side-effects gating
    side_effects: bool = False
    allow_side_effects: bool = False
    require_confirm_field: Optional[str] = None

    # Redaction
    redact_secrets: bool = True

    # Caching (optional)
    cache_enabled: bool = False
    cache_ttl_sec: Optional[int] = None  # None = per-run
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

# Utilities
_SECRET_PATTERNS = [
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)\b\s*[:=]\s*([^\s,;]+)"
    ),
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


def safe_json_loads(s: str) -> Dict[str, Any]:
    try:
        v = json.loads(s or "")
        return v if isinstance(v, dict) else {"__raw__": v}
    except Exception:
        return {"__raw__": s}


def sanitize_args_for_cache(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Remove unstable / non-semantic keys so caching is useful and safe.
    """
    if not isinstance(args, dict):
        return {}
    drop = {"confirm", "nonce", "timestamp", "ts", "request_id"}
    out: Dict[str, Any] = {}
    for k, v in args.items():
        if not isinstance(k, str):
            continue
        if k in drop:
            continue
        if k.startswith("_"):
            continue
        out[k] = v
    return out


def fingerprint(tool_name: str, args: Dict[str, Any]) -> str:
    payload = tool_name + "|" + stable_json_dumps(args)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _light_validate(schema: Dict[str, Any], args: Dict[str, Any]) -> Tuple[bool, str]:
    if not schema:
        return True, ""
    root_type = schema.get("type")
    if root_type and root_type != "object":
        return False, f"Schema root type must be object, got {root_type}"
    if not isinstance(args, dict):
        return False, "Tool arguments must be a JSON object"

    required = schema.get("required", []) or []
    for k in required:
        if k not in args:
            return False, f"Missing required field: '{k}'"

    props = schema.get("properties", {}) or {}
    allow_extra = schema.get("additionalProperties", True)
    if allow_extra is False:
        extras = set(args.keys()) - set(props.keys())
        if extras:
            return False, f"Unexpected fields: {sorted(extras)}"

    type_map = {
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "object": dict,
        "array": list,
    }

    for k, v in args.items():
        if k not in props:
            continue
        expected = props[k].get("type")
        if not expected:
            continue
        py_t = type_map.get(expected)
        if py_t and not isinstance(v, py_t):
            return False, f"Field '{k}' should be '{expected}', got '{type(v).__name__}'"

    return True, ""


def validate_against_schema(schema: Dict[str, Any], args: Dict[str, Any]) -> Tuple[bool, str]:
    if not schema:
        return True, ""
    if jsonschema is not None:
        try:
            jsonschema.validate(instance=args, schema=schema)
            return True, ""
        except Exception as e:
            return False, f"Schema validation failed: {e}"
    return _light_validate(schema, args)


def schema_repair_hints(schema: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    props = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []
    allow_extra = schema.get("additionalProperties", True)
    allowed = sorted([k for k in props.keys() if isinstance(k, str)])

    missing = [k for k in required if k not in args]
    extras: List[str] = []
    if allow_extra is False:
        extras = sorted(list(set(args.keys()) - set(props.keys())))

    return {
        "allowed_keys": allowed[:50],
        "required_keys": list(required)[:50],
        "missing_required": missing[:50],
        "unexpected_keys": extras[:50],
        "additionalProperties": allow_extra,
    }


def build_envelope(
    *,
    ok: bool,
    tool: str,
    text: str,
    urls: Optional[List[str]] = None,
    file_ids: Optional[List[str]] = None,
    data: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
    # stable machine-readable code ( it will helpful for llm to understand error properly )
    error_code: Optional[str] = None,
    fp: Optional[str] = None,
    cache_hit: bool = False,
    cached_age_ms: Optional[int] = None,
    truncated: bool = False,
    size_chars: Optional[int] = None,
) -> str:
    m = dict(meta or {})
    if error_code:
        m["error_code"] = error_code
    if fp:
        m["fp"] = fp
    if cache_hit:
        m["cache_hit"] = True
    if cached_age_ms is not None:
        m["cached_age_ms"] = int(cached_age_ms)
    if truncated:
        m["truncated"] = True
    if size_chars is not None:
        m["size_chars"] = int(size_chars)

    payload = {
        "ok": ok,
        "tool": tool,
        "text": (text or "").strip(),
        "urls": urls or [],
        "file_ids": file_ids or [],
        "data": data or {},
        "error": error,
        "meta": m,
    }
    return stable_json_dumps(payload)

# Bounded per-run cache (LRU)
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
    """
    Simple LRU cache keyed by fingerprint, storing envelope strings only.
    Uses global bounded budgets but respects per-tool TTL.
    """
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
        
        # LRU: move to end
        ent.last_access_ms = now_ms
        self._store.move_to_end(key)
        return ent

    def delete(self, key: str) -> None:
        ent = self._store.pop(key, None)
        if ent:
            self.total_bytes = max(0, self.total_bytes - int(ent.size_bytes))

    def put(self, key: str, entry: _CacheEntry, *, max_entries: int, max_total_bytes: int) -> None:
        # overwrite
        if key in self._store:
            self.delete(key)
        self._store[key] = entry
        self._store.move_to_end(key)
        self.total_bytes += int(entry.size_bytes)

        # eviction as simple as it is
        while len(self._store) > max_entries or self.total_bytes > max_total_bytes:
            k, _ent = self._store.popitem(last=False)
            self.total_bytes = max(0, self.total_bytes - int(_ent.size_bytes))


class GrokToolExecutor:
    """
    Concurrency-safe executor:
    - budgets + repeat detection are atomic
    - stable envelopes
    - schema repair hints
    - bounded cache
    """

    def __init__(
        self,
        *,
        specs: Sequence[CustomToolSpec],
        tool_runner: Any,
        default_policy: Optional[ToolPolicy] = None,
        policy_overrides: Optional[Dict[str, ToolPolicy]] = None,
    ) -> None:
        self.spec_by_name: Dict[str, CustomToolSpec] = {s.name: s for s in (specs or [])}
        self.tool_runner = tool_runner
        self.state = ToolExecState()
        self.default_policy = default_policy or ToolPolicy()
        self.policy_overrides = policy_overrides or {}
        self.cache = ToolCache()
        self._lock = asyncio.Lock()

    def policy_for(self, tool_name: str) -> ToolPolicy:
        p = self.policy_overrides.get(tool_name)
        if p:
            return p

        base = self.default_policy.model_copy(deep=True)

        # heuristic side-effect detection ( reduce overlooking )
        if tool_name.startswith(
            ("create_", "update_", "delete_", "run_", "save_", "upload_", "send_")
        ):
            base.side_effects = True
            base.require_confirm_field = base.require_confirm_field or "confirm"

        return base

    async def execute_one(
        self,
        *,
        ctx: Context,
        app_state: AppState,
        tool_name: str,
        raw_args: Any,
        notify: Optional[Notify],
        round_no: int,
    ) -> str:
        if tool_name not in self.spec_by_name:
            return build_envelope(
                ok=False,
                tool=tool_name,
                text="Tool not found in custom tool allowlist.",
                error=f"Unknown tool '{tool_name}'",
                error_code="TOOL_NOT_ALLOWED",
                meta={"round": round_no},
            )

        if self.tool_runner is None:
            raise GrokChatError("custom tools requested but no tool_runner provided")

        policy = self.policy_for(tool_name)

        # normalize args
        args = normalize_tool_args(raw_args)
        if not isinstance(args, dict):
            args = {"__raw": raw_args}

        # fingerprint  for cache + loop detection
        cache_args = sanitize_args_for_cache(args)
        fp = fingerprint(tool_name, cache_args)

        # args size guard
        arg_bytes = len(stable_json_dumps(args).encode("utf-8"))
        if arg_bytes > policy.max_arg_bytes:
            return build_envelope(
                ok=False,
                tool=tool_name,
                text="Tool arguments too large. Reduce input size or summarize before calling the tool.",
                error=f"Args size {arg_bytes} exceeds max_arg_bytes={policy.max_arg_bytes}",
                error_code="ARGS_TOO_LARGE",
                fp=fp,
                meta={"round": round_no, "arg_bytes": arg_bytes},
            )

        # schema validate
        schema = self.spec_by_name[tool_name].parameters_schema or {}
        ok, msg = validate_against_schema(schema, args)
        if not ok:
            hints = schema_repair_hints(schema, args)
            return build_envelope(
                ok=False,
                tool=tool_name,
                text="Invalid tool arguments. Fix the schema issues and retry ONCE; otherwise proceed without this tool.",
                error=msg,
                error_code="SCHEMA_INVALID",
                fp=fp,
                meta={"round": round_no, "repair": hints},
            )

        # side-effect gating
        if policy.side_effects and not policy.allow_side_effects:
            return build_envelope(
                ok=False,
                tool=tool_name,
                text="This tool can have side effects and is disabled. Ask the user for confirmation or propose a safe alternative.",
                error="SIDE_EFFECTS_DISABLED",
                error_code="SIDE_EFFECTS_DISABLED",
                fp=fp,
                meta={"round": round_no, "side_effects": True},
            )

        if policy.side_effects and policy.require_confirm_field:
            cf = policy.require_confirm_field
            if args.get(cf) is not True:
                return build_envelope(
                    ok=False,
                    tool=tool_name,
                    text=f"This tool can have side effects. Set '{cf}: true' to confirm execution, or proceed without it.",
                    error=f"Missing confirmation field '{cf}: true'",
                    error_code="CONFIRMATION_REQUIRED",
                    fp=fp,
                    meta={"round": round_no, "side_effects": True},
                )

        # cache check (lock protected)
        if policy.cache_enabled and (not policy.side_effects):
            now_ms = int(time.time() * 1000)
            async with self._lock:
                ent = self.cache.get(fp, now_ms)
            if ent:
                cached = safe_json_loads(ent.payload)
                meta = cached.get("meta") if isinstance(cached.get("meta"), dict) else {}
                meta["round"] = round_no
                meta["cache_hit"] = True
                meta["cached_age_ms"] = now_ms - ent.created_ms
                cached["meta"] = meta
                return stable_json_dumps(cached)

        # --- atomic budgets + repeat protection + reserve slot ---
        async with self._lock:
            if self.state.total_calls >= policy.max_total_tool_calls:
                return build_envelope(
                    ok=False,
                    tool=tool_name,
                    text="Tool budget exceeded. Use existing results and proceed to a final answer.",
                    error=f"Exceeded max_total_tool_calls={policy.max_total_tool_calls}",
                    error_code="MAX_TOTAL_TOOL_CALLS",
                    fp=fp,
                    meta={"round": round_no, "total_calls": self.state.total_calls},
                )

            tool_count = self.state.per_tool_calls.get(tool_name, 0)
            if tool_count >= policy.max_calls_per_chat:
                return build_envelope(
                    ok=False,
                    tool=tool_name,
                    text="Tool call limit reached for this tool. Use existing results and proceed to a final answer.",
                    error=f"Exceeded max_calls_per_chat={policy.max_calls_per_chat}",
                    error_code="MAX_CALLS_PER_TOOL",
                    fp=fp,
                    meta={"round": round_no, "tool_calls": tool_count},
                )

            fp_count = self.state.bump_fp(fp)
            if fp_count > policy.max_repeat_same_call:
                return build_envelope(
                    ok=False,
                    tool=tool_name,
                    text="Repeated identical tool call detected. Stop calling tools and finalize using existing results.",
                    error=f"Repeated same call > {policy.max_repeat_same_call}",
                    error_code="REPEAT_CALL_DETECTED",
                    fp=fp,
                    meta={"round": round_no, "repeat_count": fp_count},
                )

            # reserve this call slot
            self.state.bump_tool(tool_name)

        # execute
        _safe_notify(notify, ctx, f"Using tool: {tool_name}")

        t0 = time.perf_counter()
        try:
            coro = self.tool_runner.run_tool(ctx, app_state, tool_name, args, notify)
            out = await asyncio.wait_for(coro, timeout=policy.timeout_sec)
            dt_ms = int((time.perf_counter() - t0) * 1000)

            text = getattr(out, "text", None)
            urls = getattr(out, "urls", None)
            file_ids = getattr(out, "file_ids", None)

            if text is None:
                text = stable_json_dumps(out)

            if policy.redact_secrets:
                text = redact_text(str(text))

            truncated = False
            if len(text) > policy.max_output_chars_for_model:
                text = text[: policy.max_output_chars_for_model] + "\n\n...[TRUNCATED]"
                truncated = True

            result_key = f"r{round_no}:{tool_name}:{fp[:8]}"

            payload = build_envelope(
                ok=True,
                tool=tool_name,
                text=text,
                urls=list(urls or []),
                file_ids=list(file_ids or []),
                fp=fp,
                truncated=truncated,
                size_chars=len(text),
                meta={"round": round_no, "duration_ms": dt_ms, "result_key": result_key},
            )

            # cache store (lock protected)
            if policy.cache_enabled and (not policy.side_effects):
                now_ms = int(time.time() * 1000)
                b = len(payload.encode("utf-8"))
                if b <= policy.cache_max_entry_bytes:
                    entry = _CacheEntry(
                        payload=payload,
                        created_ms=now_ms,
                        last_access_ms=now_ms,
                        size_bytes=b,
                        ttl_sec=policy.cache_ttl_sec,
                    )
                    async with self._lock:
                        self.cache.put(
                            fp,
                            entry,
                            max_entries=policy.cache_max_entries,
                            max_total_bytes=policy.cache_max_total_bytes,
                        )

            return payload

        except asyncio.TimeoutError:
            return build_envelope(
                ok=False,
                tool=tool_name,
                text="Tool timed out. Consider proceeding without it or trying a lighter query.",
                error=f"Timeout after {policy.timeout_sec}s",
                error_code="TOOL_TIMEOUT",
                fp=fp,
                meta={"round": round_no, "timeout_sec": policy.timeout_sec},
            )

        except Exception as e:
            logger.bind(ctx=ctx, tool=tool_name).exception(f"Tool execution error: {e}")
            return build_envelope(
                ok=False,
                tool=tool_name,
                text="Tool execution failed. Proceed using existing results or adjust the request.",
                error=str(e),
                error_code="TOOL_EXECUTION_FAILED",
                fp=fp,
                meta={"round": round_no},
            )