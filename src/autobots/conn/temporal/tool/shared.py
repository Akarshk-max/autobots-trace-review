from typing import Any, Dict, Optional
from pydantic import BaseModel

class ToolWorkflowInput(BaseModel):
    tool_run_id: str
    tool_name: str
    tool_args: Dict[str, Any] = {}
    user_id: str
    space_id: str | None
    trace_id: Optional[str] = None
    access_token: Optional[str] = None
    start_to_close_timeout_minutes: int = 30
    output_text_to_file_char_limit: int = 50000
    webhook: Optional[Dict[str, Any]] = None


class ToolFinalizeInput(BaseModel):
    tool_run_id: str
    user_id: str
    reason: str
