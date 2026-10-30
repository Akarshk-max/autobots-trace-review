from typing import List, Optional

from pydantic import BaseModel, Field

from src.autobots.action.action.action_doc_model import ActionDoc, ActionFind, ActionLiteDoc
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.event_result.event_result_model import (
    EventResultCreate,
    EventResultDoc,
    EventResultDocSearch,
    EventResultFind,
    EventResultLiteDoc,
    EventResultUpdate,
    EventType,
)


class QueuedInputs(BaseModel):
    """Tracks queued inputs for sequential execution in the same conversation context."""
    inputs: List[DataBlock] = Field(default_factory=list)
    processed_count: int = 0
    total_count: int = 0
    version_number: int = 0


class ActionResultUpdate(EventResultUpdate):
    """
    Input from User to update Action Result
    """

    result: Optional[ActionDoc | ActionLiteDoc] = None
    queued_inputs: Optional[QueuedInputs] = None


class ActionResultCreate(EventResultCreate):
    type: EventType = EventType.action_run.value
    result: ActionDoc | ActionLiteDoc
    parent_action_result_id: Optional[str] = None


class ActionResultFind(EventResultFind):
    result: ActionFind | None = None


class ActionResultDoc(EventResultDoc):
    result: ActionDoc | ActionLiteDoc
    queued_inputs: Optional[QueuedInputs] = None

class ActionResultLiteDoc(EventResultLiteDoc):
    result: Optional[ActionLiteDoc] = None

class ActionResultDocsFound(BaseModel):
    docs: List[ActionResultLiteDoc]
    total_count: int
    limit: int
    offset: int


class ActionResultDocSearch(EventResultDocSearch):
    type: EventType = EventType.action_run
