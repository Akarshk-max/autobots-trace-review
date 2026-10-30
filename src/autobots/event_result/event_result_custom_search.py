import re
from typing import Any, Dict, List

from bson import ObjectId

from src.autobots.event_result.event_result_model import EventType

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def _is_hex(s: str) -> bool:
    return bool(s) and bool(_HEX_RE.match(s))


def build_event_results_custom_search_filter(
    *,
    user_id: str,
    event_type: EventType,
    q: str,
    searchable_paths: List[str] | None = None,
    include_id_prefix_suffix: bool = True,
) -> Dict[str, Any]:
    q = (q or "").strip()
    if not q:
        return {"user_id": user_id, "type": event_type.value}

    if searchable_paths is None:
        searchable_paths = ["name", "status", "result.name", "result.type"]

    tokens = [t for t in re.split(r"\s+", q) if t]

    and_clauses: List[Dict[str, Any]] = []
    for token in tokens:
        escaped = re.escape(token)
        or_clauses: List[Dict[str, Any]] = [
            {path: {"$regex": escaped, "$options": "i"}} for path in searchable_paths
        ]

        if include_id_prefix_suffix and _is_hex(token) and len(token) == 24:
            try:
                oid = ObjectId(token)
                or_clauses.append({"_id": oid})
            except Exception:
                pass

        and_clauses.append({"$or": or_clauses})

    return {
        "user_id": user_id,
        "type": event_type.value,
        "$and": and_clauses,
    }
