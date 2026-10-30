import hashlib
import json
import time
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


def _now_ms() -> int:
    return int(time.time() * 1000)


def safe_json_loads(s: str) -> Dict[str, Any]:
    try:
        v = json.loads(s or "")
        return v if isinstance(v, dict) else {"__raw__": v}
    except Exception:
        return {"__raw__": s}

# this is responsible for each round only
class StopReason(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: str = "UNKNOWN"  # e.g. MAX_ROUNDS, REPEAT_CALL_DETECTED, MODEL_DONE, TIMEOUT_STORM
    detail: Optional[str] = None
    round_no: Optional[int] = None
    tool: Optional[str] = None
    error_code: Optional[str] = None


# storing information of each tool call with unique fingerprint
class ToolCallRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    tool: str
    raw_args: Any = None
    norm_args: Dict[str, Any] = Field(default_factory=dict)
    fp: Optional[str] = None


# here it is not very helpful for llm to understand and proceed further 
class ToolResultRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    tool: str
    ok: bool = False
    error: Optional[str] = None
    error_code: Optional[str] = None

    # model-facing payload summary
    text_preview: str = ""
    urls: List[str] = Field(default_factory=list)
    file_ids: List[str] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)

    fp: Optional[str] = None
    cache_hit: bool = False
    cached_age_ms: Optional[int] = None


class RoundRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    round_no: int
    assistant_text: str = ""
    tool_calls: List[ToolCallRecord] = Field(default_factory=list)
    tool_results: List[ToolResultRecord] = Field(default_factory=list)


class RunTrace(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    started_at_ms: int = Field(default_factory=_now_ms)
    finished_at_ms: Optional[int] = None

    rounds: List[RoundRecord] = Field(default_factory=list)
    stop_reason: Optional[StopReason] = None

    # lightweight budgets snapshot
    total_tool_calls: int = 0
    per_tool_calls: Dict[str, int] = Field(default_factory=dict)

    # observability + no-progress
    metrics: Dict[str, Any] = Field(default_factory=dict)
    progress_hashes: List[str] = Field(default_factory=list)

    def ensure_round(self, round_no: int) -> RoundRecord:
        for r in self.rounds:
            if r.round_no == round_no:
                return r
        rr = RoundRecord(round_no=round_no)
        self.rounds.append(rr)
        return rr

    def set_stop(
        self,
        *,
        kind: str,
        detail: Optional[str] = None,
        round_no: Optional[int] = None,
        tool: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> None:
        self.stop_reason = StopReason(
            kind=kind, detail=detail, round_no=round_no, tool=tool, error_code=error_code
        )

    def finish(self) -> None:
        self.finished_at_ms = _now_ms()
        if "total_ms" not in self.metrics:
            self.metrics["total_ms"] = int(self.finished_at_ms - self.started_at_ms)

    def add_progress_hash(self, h: str) -> None:
        if h:
            self.progress_hashes.append(h)

    @staticmethod
    def compute_progress_hash(rr: "RoundRecord") -> str:
        parts: List[str] = []
        for tr in rr.tool_results:
            if not tr.ok:
                continue
            parts.append(tr.tool)
            parts.append((tr.text_preview or "")[:300])
            if tr.urls:
                parts.append("|".join([str(u) for u in tr.urls[:3]]))
            if tr.file_ids:
                parts.append("|".join([str(f) for f in tr.file_ids[:3]]))

        raw = "\n".join(parts).strip()
        if not raw:
            return ""
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def parse_tool_envelope(payload: str) -> Dict[str, Any]:
        return safe_json_loads(payload)

    @staticmethod
    def envelope_to_record(payload: str) -> ToolResultRecord:
        d = safe_json_loads(payload)
        meta = d.get("meta") if isinstance(d.get("meta"), dict) else {}
        tool = str(d.get("tool") or "")
        ok = bool(d.get("ok") is True)
        text = str(d.get("text") or "")
        urls = d.get("urls") if isinstance(d.get("urls"), list) else []
        file_ids = d.get("file_ids") if isinstance(d.get("file_ids"), list) else []
        err = d.get("error")
        error_code = meta.get("error_code") or (err if isinstance(err, str) else None)

        cache_hit = bool(meta.get("cache_hit") is True)
        cached_age_ms = meta.get("cached_age_ms")
        fp = meta.get("fp")

        preview = text.strip()
        if len(preview) > 900:
            preview = preview[:900] + "\n...[TRUNCATED PREVIEW]"

        return ToolResultRecord(
            tool=tool,
            ok=ok,
            error=str(err) if err is not None else None,
            error_code=str(error_code) if error_code is not None else None,
            text_preview=preview,
            urls=[str(u) for u in urls[:5]],
            file_ids=[str(f) for f in file_ids[:5]],
            meta=meta,
            fp=str(fp) if fp else None,
            cache_hit=cache_hit,
            cached_age_ms=int(cached_age_ms) if isinstance(cached_age_ms, (int, float)) else None,
        )