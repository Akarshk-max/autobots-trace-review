import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


def _now_ms() -> int:
    return int(time.time() * 1000)


class StopReason(BaseModel):
    model_config = ConfigDict(extra="allow")
    kind: str = "UNKNOWN"
    detail: Optional[str] = None
    round_no: Optional[int] = None
    tool: Optional[str] = None
    error_code: Optional[str] = None


class ToolCallRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    tool: str
    raw_args: Any = None
    parsed_args: Dict[str, Any] = Field(default_factory=dict)
    id: Optional[str] = None


class ToolResultRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    tool: str
    ok: bool = False
    output_preview: str = ""
    error_code: Optional[str] = None


class RoundRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    round_no: int
    assistant_text: str = ""
    reasoning_preview: Optional[str] = None
    tool_calls: List[ToolCallRecord] = Field(default_factory=list)
    tool_results: List[ToolResultRecord] = Field(default_factory=list)


class RunTrace(BaseModel):
    model_config = ConfigDict(extra="allow")

    started_at_ms: int = Field(default_factory=_now_ms)
    finished_at_ms: Optional[int] = None

    rounds: List[RoundRecord] = Field(default_factory=list)
    stop_reason: Optional[StopReason] = None

    total_tool_calls: int = 0
    per_tool_calls: Dict[str, int] = Field(default_factory=dict)

    def ensure_round(self, n: int) -> RoundRecord:
        for r in self.rounds:
            if r.round_no == n:
                return r
        rr = RoundRecord(round_no=n)
        self.rounds.append(rr)
        return rr

    def set_stop(self, *, kind: str, detail: Optional[str] = None, round_no: Optional[int] = None,
                 tool: Optional[str] = None, error_code: Optional[str] = None) -> None:
        self.stop_reason = StopReason(kind=kind, detail=detail, round_no=round_no, tool=tool, error_code=error_code)

    def finish(self) -> None:
        self.finished_at_ms = _now_ms()
