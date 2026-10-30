import json
from collections import Counter
from typing import Any, Literal, Type

import jmespath
from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel, Field, PrivateAttr, model_validator

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import (
    ActionABC,
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
    ActionInputType,
    ActionOutputType,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify

# Item types that are filtered out of every query result: they carry opaque
# encrypted_content that has no semantic value for the model and would waste tokens.
_SKIP_ITEM_TYPES: set[str] = {"reasoning", "compaction"}


class ChatHistoryQueryReq(BaseModel):
    """Query stored OpenAI Responses v2 chat history.

    Notes
    -----
    - The backing dataset (turn_history) is injected by the backend at runtime from
      the action's `input_archive`. Each turn is one
      ActionText2TextLlmResponsesOpenaiV2.run_action() call.
    - Reasoning and compaction items are filtered out of every result: their
      `encrypted_content` is opaque state with no semantic search value.
    """

    # Turn-level filtering
    turn: int | None = None
    turns: list[int] | None = None
    turn_min: int | None = None
    turn_max: int | None = None

    # Message-level filtering.
    # "user" / "assistant" match messages that have a `role` field.
    # "tool" matches in_turn_messages items (function_call, function_call_output,
    # file_search_call, web_search_call, code_interpreter_call, ...) which have no
    # `role` field. When roles is None, everything is returned.
    roles: list[Literal["user", "assistant", "tool"]] | None = None
    text_query: str | None = None

    # If text_query matches a message within a turn, return the entire turn
    # (user + assistant + tool blocks) so the model gets full context.
    # When False, text_query instead filters at the message level: only
    # matching messages within matching turns are returned.
    return_full_turn: bool = True

    include_pinned: bool = False

    fields: list[str] | None = None
    jmespath_query: str | None = None

    sort_by: Literal["turn"] | None = "turn"
    descending: bool = True

    # Hard cap: never allow more than 10 turns returned per call.
    limit: int = Field(default=10, ge=1, le=10)
    offset: int = Field(default=0, ge=0)

    _turn_history: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _pinned_messages: list[dict[str, Any]] = PrivateAttr(default_factory=list)

    @model_validator(mode="after")
    def enforce_turn_limits(self) -> "ChatHistoryQueryReq":
        # If the model tries to explicitly request many turns in one call, reject it.
        if self.turns is not None and len(self.turns) > 10:
            raise ValueError("turns list too large: max 10 turns per tool call")
        return self

    def set_turn_history(
        self,
        *,
        turn_history: list[dict[str, Any]] | None,
        pinned_messages: list[dict[str, Any]] | None = None,
    ) -> "ChatHistoryQueryReq":
        self._turn_history = [dict(x) for x in (turn_history or []) if isinstance(x, dict)]
        self._pinned_messages = [dict(x) for x in (pinned_messages or []) if isinstance(x, dict)]
        return self

    def get_turn_history(self) -> list[dict[str, Any]]:
        return list(self._turn_history)

    def get_pinned_messages(self) -> list[dict[str, Any]]:
        return list(self._pinned_messages)


class ChatHistoryQueryRes(BaseModel):
    total_count: int
    returned_count: int
    results: Any
    summary: dict[str, Any] = Field(default_factory=dict)


class ActionChatHistoryQueryOpenai(
    ActionABC[ChatHistoryQueryReq, ChatHistoryQueryReq, ChatHistoryQueryReq, DataBlock, DataBlocks]
):
    type = ActionType.action_text2text_chat_history_query_openai

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return ChatHistoryQueryReq

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return ChatHistoryQueryReq

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return ChatHistoryQueryReq

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
Query persisted OpenAI Responses chat history by turn.

The backend stores full chat history out-of-band in `input_archive` so only the
last N turns are sent to the model in the visible context window. Use this tool
to retrieve older turns verbatim, including tool calls and their outputs.

Reasoning and compaction items are always excluded from results: their
encrypted_content is opaque state with no semantic value.

Hard limits
-----------
- Maximum 10 turns returned per call (limit<=10). Use pagination with offset.
- If providing an explicit `turns` list, it may contain at most 10 items.

Input (examples)
----------------
{
  "turn": 3,
  "limit": 1
}

{
  "turn_min": 1,
  "turn_max": 10,
  "text_query": "OAuth",
  "return_full_turn": true,
  "limit": 5
}

{
  "turn": 7,
  "roles": ["tool"],
  "limit": 1
}

Turn selection
--------------
All turn filters AND-combine. Bounds are inclusive. If none are set,
every stored turn is eligible (subject to limit/offset).

- turn (int): return only this exact turn number.
- turns (list[int], max 10): return only these turn numbers, in any order.
- turn_min (int): skip turns with turn < turn_min (inclusive lower bound).
- turn_max (int): skip turns with turn > turn_max (inclusive upper bound).
  Use turn_min + turn_max together to fetch a contiguous range, e.g.
  {"turn_min": 20, "turn_max": 30} returns turns 20..30.

Message-level filtering
-----------------------
- roles (list[str] | None): restrict which messages inside each turn are
  returned. Default None returns everything.
    - "user":      messages with role=user
    - "assistant": messages with role=assistant
    - "tool":      in_turn_messages items with no role field (function_call,
                   function_call_output, mcp_call, mcp_list_tools, ...)
    - Combinations like ["user", "tool"] are supported.
- text_query (str | None): case-insensitive substring search across each
  message's text/content/arguments/output.
- return_full_turn (bool, default true): on a text_query match, return the
  entire turn (full context). If false, only matching messages within
  matching turns are returned.

Pinned prefix
-------------
- include_pinned (bool, default false): if true, also return the pinned
  prefix (system + first-turn instructions) as a synthetic turn 0 with
  {"turn": 0, "source": "pinned_messages"}.

Ordering & pagination
---------------------
- sort_by ("turn" | None, default "turn"): sort turns by turn number.
  None leaves insertion order.
- descending (bool, default true): newest turn first. Set false for
  oldest-first.
- limit (int, default 10, max 10): page size.
- offset (int, default 0): skip this many turns before taking a page.
  Use offset + limit to paginate through large result sets.

Advanced
--------
- fields (list[str] | None): per-turn JMESPath field projection; keep
  only the named top-level keys from each returned turn.
- jmespath_query (str | None): apply a JMESPath expression to each
  returned turn after pagination (runs after fields projection).

Output
------
{
  "total_count": 12,
  "returned_count": 5,
  "results": [ {"turn": 7, "messages": [...]}, ... ],
  "summary": {"turns": [..], "message_roles": {"user": 7, "assistant": 7, "tool": 3}}
}
"""

    @staticmethod
    def _safe_jmespath_search(query: str, data: Any) -> Any:
        try:
            return jmespath.search(query, data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JMESPath query: {str(exc)}")

    @staticmethod
    def _normalize_message(raw: Any) -> dict[str, Any] | None:
        if hasattr(raw, "model_dump"):
            try:
                raw = raw.model_dump(exclude_none=True)
            except Exception:
                return None
        if isinstance(raw, dict):
            # Filter out reasoning / compaction items: opaque encrypted_content,
            # no semantic value for the model.
            item_type = str(raw.get("type") or "").lower()
            if item_type in _SKIP_ITEM_TYPES:
                return None
            return dict(raw)
        return None

    @staticmethod
    def _message_text_haystack(message: dict[str, Any]) -> str:
        parts: list[str] = []
        role = message.get("role")
        if role is not None:
            parts.append(str(role))

        item_type = message.get("type")
        if item_type is not None:
            parts.append(str(item_type))

        name = message.get("name")
        if name is not None:
            parts.append(str(name))

        content = message.get("content")
        if content is None:
            # Tool-style items expose arguments/output instead of content.
            for alt_key in ("arguments", "output", "query", "call_id"):
                alt = message.get(alt_key)
                if alt is not None:
                    try:
                        parts.append(json.dumps(alt, ensure_ascii=False, default=str))
                    except Exception:
                        parts.append(str(alt))
            return "\n".join(parts).lower()

        try:
            parts.append(json.dumps(content, ensure_ascii=False, default=str))
        except Exception:
            parts.append(str(content))

        return "\n".join(parts).lower()

    @staticmethod
    def _message_matches_roles(
        message: dict[str, Any],
        roles: list[Literal["user", "assistant", "tool"]] | None,
    ) -> bool:
        if not roles:
            return True

        allowed = {str(role).lower() for role in roles}
        role = str(message.get("role") or "").lower()

        if role:
            return role in allowed

        return "tool" in allowed

    @classmethod
    def _filter_and_match_turn(
        cls,
        turn: dict[str, Any],
        req: ChatHistoryQueryReq,
    ) -> dict[str, Any] | None:
        messages = turn.get("messages")
        if not isinstance(messages, list):
            return None

        role_scoped: list[dict[str, Any]] = []
        for raw in messages:
            message = cls._normalize_message(raw)
            if message is None:
                continue
            if cls._message_matches_roles(message, req.roles):
                role_scoped.append(message)

        if not role_scoped:
            return None

        if req.text_query:
            needle = req.text_query.casefold()
            matching = [
                message
                for message in role_scoped
                if needle in cls._message_text_haystack(message).casefold()
            ]

            if not matching:
                return None

            result_messages = role_scoped if req.return_full_turn else matching
        else:
            result_messages = role_scoped

        out = dict(turn)
        out["messages"] = result_messages
        return out

    @staticmethod
    def _sort_turns(turns: list[dict[str, Any]], req: ChatHistoryQueryReq) -> list[dict[str, Any]]:
        if not req.sort_by:
            return turns

        def sort_key(item: dict[str, Any]):
            if req.sort_by == "turn":
                return int(item.get("turn") or -1)
            return int(item.get(req.sort_by) or -1)

        return sorted(turns, key=sort_key, reverse=req.descending)

    @classmethod
    def _project_item(cls, item: Any, fields: list[str] | None) -> Any:
        if not fields or not isinstance(item, dict):
            return item

        projected: dict[str, Any] = {}
        for field in fields:
            projected[field] = cls._safe_jmespath_search(field, item)
        return projected

    @classmethod
    def _build_summary(cls, turns: list[dict[str, Any]]) -> dict[str, Any]:
        roles_counter = Counter()
        turns_seen: list[int] = []

        for turn in turns:
            try:
                turns_seen.append(int(turn.get("turn")))
            except Exception:
                continue

            for msg in turn.get("messages") or []:
                if isinstance(msg, dict):
                    role = str(msg.get("role") or "").lower()
                    if role:
                        roles_counter[role] += 1
                    else:
                        # in_turn_messages items without a role field
                        roles_counter["tool"] += 1

        return {
            "turns": sorted({t for t in turns_seen}),
            "message_roles": dict(roles_counter),
        }

    @classmethod
    def _execute_query(cls, req: ChatHistoryQueryReq) -> ChatHistoryQueryRes:
        turns: list[dict[str, Any]] = []

        if req.include_pinned:
            pinned = req.get_pinned_messages()
            if pinned:
                turns.append({"turn": 0, "messages": pinned, "source": "pinned_messages"})

        for raw_turn in req.get_turn_history():
            if not isinstance(raw_turn, dict):
                continue
            if "turn" not in raw_turn:
                continue
            turns.append(dict(raw_turn))

        # turn-level filters
        def turn_number(item: dict[str, Any]) -> int | None:
            try:
                return int(item.get("turn"))
            except Exception:
                return None

        filtered: list[dict[str, Any]] = []
        for item in turns:
            t = turn_number(item)
            if t is None:
                continue

            if req.turn is not None and t != req.turn:
                continue
            if req.turns and t not in set(req.turns):
                continue
            if req.turn_min is not None and t < req.turn_min:
                continue
            if req.turn_max is not None and t > req.turn_max:
                continue

            matched = cls._filter_and_match_turn(item, req)
            if matched is not None:
                filtered.append(matched)

        filtered = cls._sort_turns(filtered, req)

        summary = cls._build_summary(filtered)

        # JMESPath policy: apply per-item only (after filtering + sorting + pagination).
        total_count = len(filtered)
        paged = filtered[req.offset : req.offset + req.limit]
        results: list[Any] = []
        for item in paged:
            working: Any = item
            if req.fields:
                working = cls._project_item(working, req.fields)
            if req.jmespath_query:
                working = cls._safe_jmespath_search(req.jmespath_query, working)
            results.append(working)

        return ChatHistoryQueryRes(
            total_count=total_count,
            returned_count=len(results),
            results=results,
            summary=summary,
        )

    async def run_action(
        self,
        ctx: Context,
        action_input: DataBlock,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        try:
            result = self._execute_query(self.action_config)
            return DataBlocks(
                blocks=[
                    DataBlock(
                        text=json.dumps(
                            result.model_dump(exclude_none=True), indent=2, ensure_ascii=False
                        )
                    )
                ]
            )
        except Exception as exc:
            logger.bind(ctx=ctx).exception(
                "Error in ActionChatHistoryQueryOpenai run_action: {}", exc
            )
            return exc

    async def run_tool(
        self,
        action_config: ChatHistoryQueryReq,
        ctx: Context,
        notify: Notify | None = None,
    ) -> ChatHistoryQueryRes | Exception:
        try:
            return self._execute_query(action_config)
        except Exception as exc:
            logger.bind(ctx=ctx).exception(
                "Error in ActionChatHistoryQueryOpenai run_tool: {}", exc
            )
            return exc
