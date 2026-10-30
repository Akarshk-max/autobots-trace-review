from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict
from pydantic_extra_types.pendulum_dt import DateTime

from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.id import ID, OptionalID
from src.autobots.data_model.MongoId import MongoID
from src.autobots.data_model.space_id import OptionalSpaceId
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId
from src.autobots.event_result.event_result_model import EventResultStatus, EventType


class BatchStatus(str, Enum):
    """Rolled up from the batch's runs on read. Never stored."""

    processing = "processing"
    success = "success"
    error = "error"
    partial = "partial"  # finished, but some runs succeeded and some failed


class BatchCreate(BaseModel):
    """What a batch run launched. None of this changes once the batch starts."""

    runnable_type: EventType
    runnable_id: str
    runnable_name: str = ""
    name: str = ""
    total: int
    batch_size: int


class BatchDocCreate(UserId, OptionalSpaceId, BatchCreate, CreatedAt, UpdatedAt):
    pass


class BatchFind(OptionalID):
    runnable_type: Optional[EventType] = None
    runnable_id: Optional[str] = None
    name: Optional[str] = None


class BatchDocFind(UserId, OptionalSpaceId, BatchFind):
    user_id: str | None = None


class BatchUpdate(BaseModel):
    # Renaming is the only thing a user can change; everything else is a record
    # of what was launched.
    name: Optional[str] = None


class BatchDocUpdate(ID, UserId, BatchUpdate, UpdatedAt):
    # id + user_id are required by CRUDBase.update_one, which filters on both.
    pass


class BatchDoc(MongoID, BatchDocCreate):
    __collection__ = "Batches"

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class BatchLiteDoc(MongoID, UserId, OptionalSpaceId, CreatedAt, UpdatedAt):
    # Drives the Mongo projection in CRUDBase._find_raw - a field missing here
    # is stripped from every list response.
    runnable_type: EventType
    runnable_id: str
    runnable_name: str = ""
    name: str = ""
    total: int = 0
    batch_size: int = 0

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


def roll_up_status(counts: Dict[str, int]) -> BatchStatus:
    """A batch is only finished once no run is still going.

    `stuck` is never assigned anywhere in the codebase - it is only ever detected
    by query - so it cannot appear in these counts and is not special-cased.
    """
    if not counts:
        # The batch document is written before its runs, so briefly there are none.
        return BatchStatus.processing

    in_flight = counts.get(EventResultStatus.processing.value, 0) + counts.get(
        EventResultStatus.waiting.value, 0
    )
    if in_flight:
        return BatchStatus.processing

    succeeded = counts.get(EventResultStatus.success.value, 0)
    failed = counts.get(EventResultStatus.error.value, 0)
    if succeeded and not failed:
        return BatchStatus.success
    if failed and not succeeded:
        return BatchStatus.error
    return BatchStatus.partial


class BatchResponse(BaseModel):
    """A batch as the API returns it.

    Everything from `success` down to `status` is derived from the batch's runs
    on every read and is not stored - see roll_up_status.
    """

    batch_id: str
    user_id: str
    space_id: Optional[str] = None

    runnable_type: EventType
    runnable_id: str
    runnable_name: str = ""
    name: str = ""
    total: int = 0
    batch_size: int = 0

    success: int = 0
    error: int = 0
    processing: int = 0
    waiting: int = 0
    done: int = 0
    status: BatchStatus = BatchStatus.processing

    created_at: DateTime
    updated_at: DateTime

    @staticmethod
    def from_doc(doc: BatchLiteDoc, counts: Dict[str, int]) -> "BatchResponse":
        success = counts.get(EventResultStatus.success.value, 0)
        error = counts.get(EventResultStatus.error.value, 0)
        return BatchResponse(
            batch_id=doc.id,
            user_id=doc.user_id,
            space_id=doc.space_id,
            runnable_type=doc.runnable_type,
            runnable_id=doc.runnable_id,
            runnable_name=doc.runnable_name,
            name=doc.name,
            total=doc.total,
            batch_size=doc.batch_size,
            success=success,
            error=error,
            processing=counts.get(EventResultStatus.processing.value, 0),
            waiting=counts.get(EventResultStatus.waiting.value, 0),
            done=success + error,
            status=roll_up_status(counts),
            created_at=doc.created_at,
            updated_at=doc.updated_at,
        )


class BatchDocsFound(DocFindPage):
    docs: List[BatchResponse]
