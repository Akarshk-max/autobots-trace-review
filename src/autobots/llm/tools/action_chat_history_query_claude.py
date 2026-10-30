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
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify


class ChatHistoryQueryReq(BaseModel):
    """Query stored Claude chat history.

    Notes
    -----
    - The backing dataset (turn_history) is injected by the backend at runtime.
    - A "turn" corresponds to one ActionText2TextLlmChatclaude.run_action() call.
    """

    # Turn-level filtering
    turn: int | None = None
    turns: list[int] | None = None
    turn_min: int | None = None
    turn_max: int | None = None

    # Message-level filtering
    roles: list[Literal["user", "assistant"]] | None = None
    text_query: str | None = None

    semantic_query: str | None = None
    semantic_max_results: int = Field(default=5, ge=1, le=20)

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
        self._pinned_messages = [
            dict(x) for x in (pinned_messages or []) if isinstance(x, dict)
        ]
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


class ActionChatHistoryQueryClaude(
    ActionABC[ChatHistoryQueryReq, ChatHistoryQueryReq, ChatHistoryQueryReq, DataBlock, DataBlocks]
):
    type = ActionType.action_text2text_chat_history_query_claude

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
Query persisted Claude chat history by turn, or semantically search older
turns that are no longer in the visible context window.

The backend stores chat history out-of-band in `turn_history` so only the last
N turns are sent to the model by default. Use this tool to retrieve older
turns verbatim, or to semantically search across the whole conversation.

IMPORTANT — call this proactively, on EVERY personal or factual question,
not only ones that reference timing. The user cannot see your context window
and does not know which turns have scrolled out of it — a bare question like
"what is my cat's name?" or "remember my X?" is just as likely to be asking
about something outside your visible window as one that says "you told me
this earlier." Treat any question about a name, preference, decision, or
fact you don't currently see as a trigger to call this tool with
`semantic_query` FIRST. Never answer "I don't have that information" or
"you haven't told me that" without calling this tool first — it may have
been said earlier and simply fallen outside what you can currently see.
Only tell the user you have no record of something after this tool has
returned no results.

Two modes
---------
1. Exact/structured mode (default): filters `turn_history` in-memory by turn
   number, role, or a case-insensitive substring (`text_query`).
2. Semantic mode: set `semantic_query` to a natural-language question. The
   backend periodically indexes completed turns into a conversation-memory
   vector store (every `history_vector_store_index_every_n_turns` turns,
   default 20), so this mode can find relevant older context even when it is
   far outside the visible window and doesn't share exact wording with the
   query. `semantic_query` takes priority over turn/text_query filtering when
   set. If no turns have been indexed yet (conversation still short), this
   mode returns no results — fall back to exact/structured mode.

Hard limits
-----------
- Maximum 10 turns returned per call (limit<=10). Use pagination with offset.
- If providing an explicit `turns` list, it may contain at most 10 items.
- Semantic mode returns at most `semantic_max_results` (default 5, max 20) chunks.

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
  "semantic_query": "what did the user decide about the OAuth redirect URL earlier?",
  "semantic_max_results": 5
}

Filtering
---------
- turn / turns / turn_min / turn_max: select which turns
- roles: restrict returned messages to user/assistant
- text_query: case-insensitive substring search; if a match is found and
  return_full_turn=true, the entire turn is returned.
- semantic_query: natural-language semantic search against indexed older
  turns (see "Semantic mode" above); when set, other filters are ignored.

Advanced
--------
- fields: projection fields (JMESPath field access)
- jmespath_query: apply a per-item JMESPath expression to each returned turn after pagination

Output
------
{
  "total_count": 12,
  "returned_count": 5,
  "results": [ {"turn": 7, "messages": [...]}, ... ],
  "summary": {"turns": [..], "message_roles": {"user": 7, "assistant": 7}}
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
            return dict(raw)
        return None

    @staticmethod
    def _message_text_haystack(message: dict[str, Any]) -> str:
        parts: list[str] = []
        role = message.get("role")
        if role is not None:
            parts.append(str(role))

        content = message.get("content")
        if content is None:
            return "\n".join(parts).lower()

        try:
            parts.append(json.dumps(content, ensure_ascii=False, default=str))
        except Exception:
            parts.append(str(content))

        return "\n".join(parts).lower()

    @classmethod
    def _turn_matches_text_query(cls, turn: dict[str, Any], needle: str) -> bool:
        messages = turn.get("messages")
        if not isinstance(messages, list):
            return False

        for raw_msg in messages:
            msg = cls._normalize_message(raw_msg)
            if not msg:
                continue
            if needle in cls._message_text_haystack(msg):
                return True
        return False

    @classmethod
    def _filter_turn_messages(cls, turn: dict[str, Any], req: ChatHistoryQueryReq) -> dict[str, Any]:
        out = dict(turn)
        messages = out.get("messages")
        if not isinstance(messages, list):
            out["messages"] = []
            return out

        normalized_messages: list[dict[str, Any]] = []
        for raw in messages:
            msg = cls._normalize_message(raw)
            if msg is not None:
                normalized_messages.append(msg)

        # role filtering
        if req.roles:
            allowed = {str(r).lower() for r in req.roles}
            normalized_messages = [
                m for m in normalized_messages if str(m.get("role") or "").lower() in allowed
            ]

        # text query filtering (message-level) only when return_full_turn is False
        if req.text_query and not req.return_full_turn:
            needle = req.text_query.lower()
            normalized_messages = [
                m for m in normalized_messages if needle in cls._message_text_haystack(m)
            ]

        out["messages"] = normalized_messages
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
                    roles_counter[str(msg.get("role") or "unknown")] += 1

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

            if req.text_query:
                needle = req.text_query.lower()
                if not cls._turn_matches_text_query(item, needle):
                    continue

            filtered.append(item)

        filtered = cls._sort_turns(filtered, req)

        # If return_full_turn is False we filter messages down now.
        # If True, we still apply role filtering (it's generally safe), but not text filtering.
        filtered = [cls._filter_turn_messages(turn, req) for turn in filtered]

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
        app_state: AppState,
        action_input: DataBlock,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        try:
            result = self._execute_query(self.action_config)
            return DataBlocks(
                blocks=[
                    DataBlock(
                        text=json.dumps(result.model_dump(exclude_none=True), indent=2, ensure_ascii=False)
                    )
                ]
            )
        except Exception as exc:
            logger.bind(ctx=ctx).exception("Error in ActionChatHistoryQueryClaude run_action: {}", exc)
            return exc

    async def run_tool(
        self,
        action_config: ChatHistoryQueryReq,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> ChatHistoryQueryRes | Exception:
        try:
            return self._execute_query(action_config)
        except Exception as exc:
            logger.bind(ctx=ctx).exception("Error in ActionChatHistoryQueryClaude run_tool: {}", exc)
            return exc
