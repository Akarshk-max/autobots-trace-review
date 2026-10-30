from collections.abc import Iterable as AbcIterable
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic_extra_types.pendulum_dt import DateTime

from src.autobots.action.action.common_action_models import TextObj
from src.autobots.core.clock.clock import Clock
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.data_model.space_id import OptionalSpaceId, SpaceId
from src.autobots.data_model.updated_at import UpdatedAt


class EventType(str, Enum):
    action_run = "action_run"
    action_graph_run = "action_graph_run"
    action_team_run = "action_team_run"
    tool_run = "tool_run"
    datastore_put = "datastore_put"


class Participation(str, Enum):
    """What the people a run is shared with may do with it.

    Set by the owner when sharing. `post_as_self` is defined but refused at the
    ingress: an agent's config names its connections by id, and those ids are the
    owner's, so running as a participant needs resolution by type instead.
    """

    read_only = "read_only"
    post_as_owner = "post_as_owner"
    post_as_self = "post_as_self"


class EventResultStatus(str, Enum):
    success = "success"
    processing = "processing"
    error = "error"
    waiting = "waiting"
    stuck = "stuck"
    request_cancel = "request_cancel"
    cancelled = "cancelled"


class EventResultFind(BaseModel):
    name: Optional[str] = None
    id: Optional[str] = None
    type: Optional[EventType] = None
    status: Optional[EventResultStatus] = None
    is_saved: Optional[bool] = None
    result: Optional[Dict[str, Any]] = None
    batch_id: Optional[str] = None
    is_published: Optional[bool] = None
    # Singular: the filter builder is equality-only, so one grantee per query branch.
    shared_with: Optional[SpaceId | str] = None
    # Date range filters
    created_at_gte: Optional[datetime] = None
    created_at_lte: Optional[datetime] = None


class EventResultDocFind(EventResultFind, OptionalSpaceId):
    user_id: str | None = None


class EventResultUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[EventType] = None
    status: Optional[EventResultStatus] = None
    error_message: Optional[TextObj] = None
    is_saved: Optional[bool] = None
    result: Optional[Dict[str, Any]] = None
    file_url: Optional[str] = None
    is_published: Optional[bool] = None
    shared_with: Optional[List[SpaceId | str]] = None
    participation: Optional[Participation] = None


class EventResultDocUpdate(EventResultUpdate, UpdatedAt):
    id: str
    user_id: str
    # updated_at: datetime = datetime.now()

    # @staticmethod
    # def _make_mongo_key_safe(key: str) -> str:
    #     # Mongo rejects keys with '.' and keys starting with '$'
    #     if key.startswith("$"):
    #         key = "$" + key[1:]  # full-width dollar
    #     return key.replace(".", ". ")  # full-width dot

    @classmethod
    def _make_bson_safe(cls, value: Any) -> Any:
        """
        Only materialize *iterables* into BSON-safe containers.

        - dict -> dict (sanitize keys) + recurse
        - list/tuple/set/... -> list + recurse
        - other iterables (e.g. SerializationIterator, generators) -> list(...) + recurse
        - everything else unchanged (datetime/Decimal/UUID/etc untouched)
        """
        if value is None:
            return None

        # dict is iterable: sanitize keys + recurse values
        if isinstance(value, dict):
            out: Dict[str, Any] = {}
            for k, v in value.items():
                # ks = cls._make_mongo_key_safe(str(k))
                out[k] = cls._make_bson_safe(v)
            return out

        # common containers -> list + recurse
        if isinstance(value, (list, tuple, set, frozenset)):
            return [cls._make_bson_safe(v) for v in value]

        # IMPORTANT: handle weird iterables like SerializationIterator / generators
        # but do NOT treat strings/bytes as iterables here.
        if isinstance(value, AbcIterable) and not isinstance(
            value, (str, bytes, bytearray, memoryview)
        ):
            try:
                return [cls._make_bson_safe(v) for v in value]
            except TypeError:
                # if it's "iterable-ish" but not actually consumable, just return as-is
                return value

        # non-iterables unchanged (datetime/Decimal/UUID/etc stay exactly the same)
        return value

    def to_mongo_update(self) -> "EventResultDocUpdate":
        raw = self.model_dump(by_alias=True, exclude_none=True)
        safe = self._make_bson_safe(raw)
        return self.__class__.model_validate(safe)


class EventResultCreate(BaseModel):
    name: str = ""
    type: EventType
    status: EventResultStatus
    error_message: Optional[TextObj] = None
    is_saved: bool = False
    result: Dict[str, Any] = {}
    # Not on EventResultUpdate: a rerun must not clobber it.
    batch_id: Optional[str] = None
    is_published: bool = False
    shared_with: List[SpaceId | str] = []
    participation: Participation = Participation.read_only
    parent_action_result_id: Optional[str] = None


class EventResultDocCreate(OptionalSpaceId, EventResultCreate, UpdatedAt):
    user_id: str
    created_at: datetime = Clock.datetime_now()
    # updated_at: datetime = datetime.now()


class EventResultLiteDoc(OptionalSpaceId, BaseModel):
    id: str = Field(..., alias="_id")
    name: str = ""  # TODO: remove default value, after all Events have name populated for them
    user_id: str
    type: EventType
    status: EventResultStatus
    error_message: Optional[TextObj] = None
    is_saved: bool = False
    created_at: datetime
    updated_at: DateTime
    result: Optional[Dict[str, Any]] = None
    file_url: Optional[str] = None
    # Needed here too: this model is the projection in CRUDBase._find_raw.
    batch_id: Optional[str] = None
    is_published: bool = False
    shared_with: List[SpaceId | str] = []
    participation: Participation = Participation.read_only
    parent_action_result_id: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class EventResultDoc(EventResultDocCreate):
    __collection__ = "EventResults"

    id: str = Field(..., alias="_id")
    file_url: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class EventResultDocSearch(CollectionDocSearch):
    type: EventType
