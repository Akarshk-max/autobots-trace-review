from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.space.space_doc_model import ADMIN_ROLES, SpaceMemberDoc


class ScopeKind(str, Enum):
    personal = "personal"
    own_in_space = "own_in_space"
    whole_space = "whole_space"


class RunMetricsScope:
    """Which runs a caller may aggregate.

    Mirrors the SpaceActionGraphResults convention: a member sees their own work,
    an owner or admin sees the whole space.
    """

    def __init__(self, kind: ScopeKind, user_id: str, space_id: Optional[str] = None):
        self.kind = kind
        self.user_id = user_id
        self.space_id = space_id

    @classmethod
    def resolve(cls, user_id: str, space_member: Optional[SpaceMemberDoc]) -> "RunMetricsScope":
        if space_member is None or not space_member.space_id:
            return cls(ScopeKind.personal, user_id)
        if space_member.role in ADMIN_ROLES:
            return cls(ScopeKind.whole_space, user_id, space_member.space_id)
        return cls(ScopeKind.own_in_space, user_id, space_member.space_id)

    def match(self, start: datetime, end: datetime) -> Dict[str, Any]:
        match: Dict[str, Any] = {
            "is_root_run": {"$ne": False},
            "status": {
                "$in": [
                    EventResultStatus.success.value,
                    EventResultStatus.error.value,
                    EventResultStatus.cancelled.value,
                ]
            },
            # Half-open, so the current window and the one before it cannot both claim a run
            # whose timestamp lands exactly on the boundary between them.
            "created_at": {"$gte": start, "$lt": end},
        }
        if self.kind is ScopeKind.whole_space:
            match["space_id"] = self.space_id
        elif self.kind is ScopeKind.own_in_space:
            match["user_id"] = self.user_id
            match["space_id"] = self.space_id
        else:
            match["user_id"] = self.user_id
            match["space_id"] = None
        return match

    def generation_key(self) -> str:
        if self.kind is ScopeKind.whole_space:
            return f"space:{self.space_id}"
        return f"user:{self.user_id}"

    def cache_key(self, time_range: str, granularity: str) -> str:
        """Keyed on the named range rather than the resolved window, which moves every call
        and would never produce a hit. The entry's own TTL bounds how stale that can be."""
        return ":".join(
            [
                self.kind.value,
                self.user_id if self.kind is not ScopeKind.whole_space else "-",
                self.space_id or "-",
                time_range,
                granularity,
            ]
        )
