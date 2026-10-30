"""Owner-or-shared query construction for run results.

`EventResults` holds all three run types in one collection, so every branch must
carry `type` - without it a graph-result query returns action results shared to the
same space.
"""

from typing import List, Optional, Tuple

from src.autobots.data_model.space_id import SpaceId
from src.autobots.event_result.event_result_model import (
    EventResultDocFind,
    EventResultFind,
    EventType,
)


def build_owned_or_shared_finds(
    *,
    result_find: EventResultFind,
    user_id: str,
    user_email: Optional[str],
    space_id: Optional[str],
) -> Tuple[EventResultDocFind, Optional[List[EventResultDocFind]]]:
    """Everything I own, plus anything shared with me. `shared_with` is the opt-in."""
    doc_find = EventResultDocFind(
        user_id=user_id, space_id=space_id, **result_find.model_dump()
    )
    if not result_find.shared_with:
        return doc_find, None

    if not user_email or result_find.shared_with != user_email:
        raise ValueError("shared_with must be the caller's own email")

    doc_find.shared_with = None

    branches: List[EventResultDocFind] = []

    shared_with_me = EventResultDocFind(
        **result_find.model_dump(exclude_none=True), space_id=space_id
    )
    shared_with_me.user_id = None
    branches.append(shared_with_me)

    if space_id:
        branches.append(
            EventResultDocFind(
                type=result_find.type, shared_with=SpaceId(space_id=space_id)
            )
        )

    return doc_find, branches


def build_shared_finds_for_id(
    *,
    result_id: str,
    event_type: EventType,
    user_email: Optional[str],
    space_id: Optional[str],
) -> Tuple[EventResultDocFind, Optional[List[EventResultDocFind]]]:
    """One result shared with the caller. Owner access is a separate lookup."""
    doc_find = EventResultDocFind(
        id=result_id, type=event_type, shared_with=user_email, space_id=space_id
    )
    or_find_queries = None
    if space_id:
        or_find_queries = [
            EventResultDocFind(
                id=result_id, type=event_type, shared_with=SpaceId(space_id=space_id)
            )
        ]
    return doc_find, or_find_queries
