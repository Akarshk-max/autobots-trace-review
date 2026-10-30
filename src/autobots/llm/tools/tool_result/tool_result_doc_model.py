from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from src.autobots.event_result.event_result_model import (
    EventResultCreate,
    EventResultDoc,
    EventResultFind,
    EventResultLiteDoc,
    EventResultStatus,
    EventResultUpdate,
    EventType,
)

TERMINAL_TOOL_RUN_STATUSES = {
    EventResultStatus.success,
    EventResultStatus.error,
    EventResultStatus.cancelled,
    EventResultStatus.stuck,
}

class ToolRun(BaseModel):
    tool_name: str
    tool_args: Dict[str, Any] = {}
    output: Optional[Dict[str, Any]] = None


class ToolResultCreate(EventResultCreate):
    type: EventType = EventType.tool_run
    result: ToolRun


class ToolResultUpdate(EventResultUpdate):
    result: Optional[ToolRun] = None


class ToolRunFind(BaseModel):
    tool_name: Optional[str] = None


class ToolResultFind(EventResultFind):
    type: Optional[EventType] = EventType.tool_run
    result: Optional[ToolRunFind] = None


class ToolResultDoc(EventResultDoc):
    result: ToolRun


class ToolRunLite(BaseModel):
    """Listing view of a run: identity and inputs, without the output payload."""

    tool_name: str
    tool_args: Dict[str, Any] = {}


class ToolResultLiteDoc(EventResultLiteDoc):
    result: Optional[ToolRunLite] = None


class ToolResultDocsFound(BaseModel):
    docs: List[ToolResultLiteDoc]
    total_count: int
    limit: int
    offset: int
