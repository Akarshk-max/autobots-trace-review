from typing import Any

from src.autobots.data_model.context import Context


def _normalize_optional_id(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def build_ctx(
    *,
    user_id: str,
    trace_id: str | None = None,
    consumer_id: str | None = None,
    actor_user_id: str | None = None,
) -> Context:
    kwargs: dict[str, Any] = {
        "user_id": user_id,
        "actor_user_id": _normalize_optional_id(actor_user_id),
        "consumer_id": _normalize_optional_id(consumer_id),
    }

    normalized_trace_id = _normalize_optional_id(trace_id)
    if normalized_trace_id is not None:
        kwargs["trace_id"] = normalized_trace_id

    return Context(**kwargs)
