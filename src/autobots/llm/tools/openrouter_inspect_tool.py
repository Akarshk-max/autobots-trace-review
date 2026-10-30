import json
from collections import Counter
from typing import Any, Literal

import jmespath
from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

from src.autobots.action.action.common_action_models import DataBlocks
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


class OpenRouterInspectReq(BaseModel):
    op: Literal["chat_history", "tool_results"]

    # Chat history query
    turn: int | None = None
    turns: list[int] | None = None
    turn_min: int | None = None
    turn_max: int | None = None

    roles: list[Literal["system", "user", "assistant", "tool"]] | None = None
    text_query: str | None = None
    return_full_turn: bool = True
    include_pinned: bool = False

    semantic_query: str | None = None
    semantic_max_results: int = Field(default=5, ge=1, le=20)

    # Tool results query
    chat_turn: int | None = None
    chat_turns: list[int] | None = None
    chat_turn_min: int | None = None
    chat_turn_max: int | None = None

    step_index: int | None = None
    step_index_min: int | None = None
    step_index_max: int | None = None

    tool_names: list[str] | None = None
    tool_call_ids: list[str] | None = None
    is_error: bool | None = None

    # Shared
    fields: list[str] | None = None
    jmespath_query: str | None = None

    sort_by: Literal["turn", "chat_turn", "step_index", "name"] | None = None
    descending: bool = True

    limit: int = Field(default=20, ge=1, le=200)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_limits(self) -> "OpenRouterInspectReq":
        if self.op == "chat_history" and self.turns is not None and len(self.turns) > 50:
            raise ValueError("turns list too large: max 50")
        if self.op == "tool_results" and self.chat_turns is not None and len(self.chat_turns) > 200:
            raise ValueError("chat_turns list too large: max 200")
        return self


class OpenRouterInspectRes(BaseModel):
    total_count: int
    returned_count: int
    results: Any
    summary: dict[str, Any] = Field(default_factory=dict)


def _safe_jmespath_search(query: str, data: Any) -> Any:
    try:
        return jmespath.search(query, data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JMESPath query: {str(exc)}")


def _normalize_item(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if hasattr(raw, "model_dump"):
        try:
            raw = raw.model_dump(exclude_none=True)
        except Exception:
            return None
    if isinstance(raw, dict):
        return dict(raw)
    return None


def _message_haystack(message: dict[str, Any]) -> str:
    parts: list[str] = []
    role = message.get("role")
    if role is not None:
        parts.append(str(role))

    content = message.get("content")
    if content is not None:
        try:
            parts.append(json.dumps(content, ensure_ascii=False, default=str))
        except Exception:
            parts.append(str(content))

    return "\n".join(parts).lower()


def _project_item_fields(item: dict[str, Any], fields: list[str] | None) -> dict[str, Any]:
    if not fields:
        return item

    out: dict[str, Any] = {}
    for f in fields:
        key = str(f)
        out[key] = _safe_jmespath_search(key, item)
    return out


def execute_openrouter_inspect(
    *,
    req: OpenRouterInspectReq,
    turn_history: list[dict[str, Any]] | None,
    pinned_messages: list[dict[str, Any]] | None,
    tool_results: list[dict[str, Any]] | None,
) -> OpenRouterInspectRes:
    if req.op == "chat_history":
        normalized_turns: list[dict[str, Any]] = []
        for raw_turn in turn_history or []:
            item = _normalize_item(raw_turn)
            if not item:
                continue
            # Normalize messages into dicts
            msgs: list[dict[str, Any]] = []
            for raw_msg in item.get("messages") or []:
                msg = _normalize_item(raw_msg)
                if msg:
                    msgs.append(msg)
            normalized_turns.append({"turn": int(item.get("turn") or 0), "messages": msgs})

        turns = normalized_turns

        if req.turn is not None:
            turns = [t for t in turns if t.get("turn") == req.turn]
        if req.turns:
            allowed = set(int(x) for x in req.turns)
            turns = [t for t in turns if int(t.get("turn") or 0) in allowed]
        if req.turn_min is not None:
            turns = [t for t in turns if int(t.get("turn") or 0) >= req.turn_min]
        if req.turn_max is not None:
            turns = [t for t in turns if int(t.get("turn") or 0) <= req.turn_max]

        needle = (req.text_query or "").strip().lower()
        if needle:
            filtered: list[dict[str, Any]] = []
            for t in turns:
                msgs = t.get("messages") or []
                match_any = any(needle in _message_haystack(m) for m in msgs if isinstance(m, dict))
                if not match_any:
                    continue
                if req.return_full_turn:
                    filtered.append(t)
                else:
                    trimmed_msgs = [m for m in msgs if needle in _message_haystack(m)]
                    filtered.append({"turn": t.get("turn"), "messages": trimmed_msgs})
            turns = filtered

        if req.roles:
            allowed_roles = set(req.roles)
            role_filtered: list[dict[str, Any]] = []
            for t in turns:
                msgs = [m for m in (t.get("messages") or []) if m.get("role") in allowed_roles]
                role_filtered.append({"turn": t.get("turn"), "messages": msgs})
            turns = role_filtered

        # Sort
        turns.sort(key=lambda x: int(x.get("turn") or 0), reverse=bool(req.descending))

        total = len(turns)
        page = turns[int(req.offset) : int(req.offset) + int(req.limit)]

        # Optionally inject pinned
        if req.include_pinned:
            pinned = [m for m in (pinned_messages or []) if isinstance(m, dict)]
            if pinned:
                page = [{"turn": 0, "messages": pinned, "pinned": True}] + page

        if req.fields or req.jmespath_query:
            transformed: list[Any] = []
            for item in page:
                projected = _project_item_fields(item, req.fields)
                if req.jmespath_query:
                    projected = _safe_jmespath_search(req.jmespath_query, projected)
                transformed.append(projected)
            page = transformed  # type: ignore

        role_counter = Counter()
        for t in page:
            if not isinstance(t, dict):
                continue
            for m in t.get("messages") or []:
                if isinstance(m, dict) and m.get("role"):
                    role_counter[str(m.get("role"))] += 1

        return OpenRouterInspectRes(
            total_count=total,
            returned_count=len(page),
            results=page,
            summary={
                "turns": [t.get("turn") for t in page if isinstance(t, dict)],
                "message_roles": dict(role_counter),
            },
        )

    if req.op == "tool_results":
        normalized: list[dict[str, Any]] = []
        for raw_item in tool_results or []:
            item = _normalize_item(raw_item)
            if item:
                normalized.append(item)

        items = normalized

        if req.chat_turn is not None:
            items = [x for x in items if x.get("chat_turn") == req.chat_turn]
        if req.chat_turns:
            allowed = set(int(x) for x in req.chat_turns)
            items = [x for x in items if int(x.get("chat_turn") or 0) in allowed]
        if req.chat_turn_min is not None:
            items = [x for x in items if int(x.get("chat_turn") or 0) >= req.chat_turn_min]
        if req.chat_turn_max is not None:
            items = [x for x in items if int(x.get("chat_turn") or 0) <= req.chat_turn_max]

        if req.step_index is not None:
            items = [x for x in items if x.get("step_index") == req.step_index]
        if req.step_index_min is not None:
            items = [x for x in items if int(x.get("step_index") or 0) >= req.step_index_min]
        if req.step_index_max is not None:
            items = [x for x in items if int(x.get("step_index") or 0) <= req.step_index_max]

        if req.tool_names:
            allowed = set(n.lower() for n in req.tool_names)
            items = [x for x in items if str(x.get("name") or "").lower() in allowed]

        if req.tool_call_ids:
            allowed = set(str(x) for x in req.tool_call_ids)
            items = [x for x in items if str(x.get("tool_call_id") or "") in allowed]

        if req.is_error is not None:
            items = [x for x in items if bool(x.get("is_error")) == bool(req.is_error)]

        needle = (req.text_query or "").strip().lower()
        if needle:
            def _hay(x: dict[str, Any]) -> str:
                try:
                    return json.dumps(x, ensure_ascii=False, default=str).lower()
                except Exception:
                    return str(x).lower()

            items = [x for x in items if needle in _hay(x)]

        # Sort
        sort_by = req.sort_by or "chat_turn"
        if sort_by == "name":
            items.sort(key=lambda x: str(x.get("name") or ""), reverse=bool(req.descending))
        elif sort_by == "step_index":
            items.sort(key=lambda x: int(x.get("step_index") or 0), reverse=bool(req.descending))
        else:
            items.sort(key=lambda x: int(x.get("chat_turn") or 0), reverse=bool(req.descending))

        total = len(items)
        page_items = items[int(req.offset) : int(req.offset) + int(req.limit)]

        if req.fields or req.jmespath_query:
            transformed: list[Any] = []
            for item in page_items:
                projected = _project_item_fields(item, req.fields)
                if req.jmespath_query:
                    projected = _safe_jmespath_search(req.jmespath_query, projected)
                transformed.append(projected)
            page_items = transformed  # type: ignore

        by_name = Counter(str(x.get("name") or "") for x in page_items if isinstance(x, dict))
        by_turn = Counter(int(x.get("chat_turn") or 0) for x in page_items if isinstance(x, dict))

        return OpenRouterInspectRes(
            total_count=total,
            returned_count=len(page_items),
            results=page_items,
            summary={
                "by_name": dict(by_name),
                "chat_turns": sorted([k for k in by_turn.keys() if k]),
            },
        )

    raise HTTPException(status_code=400, detail=f"Unknown op: {req.op}")


async def openrouter_inspect(
    config: OpenRouterInspectReq,
    ctx: Context,
    user: UserORM,
    notify: Notify | None = None,
 ) -> DataBlocks | Exception:
    """Inspect OpenRouter chat state that is *not* in the current visible context window.
    Description
    ---------------
    This tool allows the model to request slices of that persisted state when
    needed (e.g., to recall earlier requirements or review a prior tool run).

    Operations
    ----------
    `config.op` selects what to inspect:
    - `chat_history`: Inspect older chat turns (turn_history).
    - `tool_results`: Inspect prior tool calls / tool outputs recorded by the runtime.

    Semantic search (op=chat_history only)
    ------------------------------------
    Set `semantic_query` to a natural-language question to search older turns
    that have been indexed into the conversation-memory vector store. This finds
    relevant older context even when it is far outside the visible window and
    doesn't share exact wording with the query. `semantic_query` takes priority
    over turn/text_query filtering when set.

    Example payloads
    ----------------
    Chat history (turn 5 only):
    `{ "op": "chat_history", "turn": 5, "limit": 1 }`

    Semantic history search:
    `{ "op": "chat_history", "semantic_query": "what did the user decide about X?", "semantic_max_results": 5 }`

    Tool results (last 10 tool calls):
    `{ "op": "tool_results", "limit": 10, "sort_by": "chat_turn", "descending": true }`

    Tool results filtered by tool name:
    `{ "op": "tool_results", "tool_names": ["daytona_sandbox"], "limit": 20 }`
    """
    raise HTTPException(
        status_code=400,
        detail="openrouter_inspect must be executed inside the OpenRouter chat runtime",
    )
