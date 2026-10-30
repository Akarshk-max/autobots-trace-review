import json
from collections import Counter
from typing import Any, Literal, Type

import jmespath
from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel, Field, PrivateAttr

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
from src.autobots.data_model.app_state import AppState

class ToolResultsQueryReq(BaseModel):
    chat_turn: int | None = None
    chat_turns: list[int] | None = None
    chat_turn_min: int | None = None
    chat_turn_max: int | None = None

    step_index: int | None = None
    step_indexes: list[int] | None = None
    step_index_min: int | None = None
    step_index_max: int | None = None

    tool_names: list[str] | None = None
    sources: list[str] | None = None
    tool_use_ids: list[str] | None = None
    is_error: bool | None = None

    text_query: str | None = None
    fields: list[str] | None = None
    jmespath_query: str | None = None

    sort_by: Literal["chat_turn", "step_index", "name", "source"] | None = "chat_turn"
    descending: bool = True

    limit: int = Field(default=20, ge=1, le=200)
    offset: int = Field(default=0, ge=0)

    _tool_results: list[dict[str, Any]] = PrivateAttr(default_factory=list)

    def set_tool_results(self, tool_results: list[dict[str, Any]] | None) -> "ToolResultsQueryReq":
        normalized_items: list[dict[str, Any]] = []

        for raw_item in tool_results or []:
            if hasattr(raw_item, "model_dump"):
                try:
                    raw_item = raw_item.model_dump(exclude_none=True)
                except Exception:
                    continue

            if isinstance(raw_item, dict):
                normalized_items.append(dict(raw_item))

        self._tool_results = normalized_items
        return self

    def get_tool_results(self) -> list[dict[str, Any]]:
        return list(self._tool_results)


class ToolResultsQueryRes(BaseModel):
    total_count: int
    returned_count: int
    results: Any
    summary: dict[str, Any] = Field(default_factory=dict)


class ActionToolResultsQueryClaude(
    ActionABC[ToolResultsQueryReq, ToolResultsQueryReq, ToolResultsQueryReq, DataBlock, DataBlocks]
):
    type = ActionType.action_text2text_tool_results_query_claude

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return ToolResultsQueryReq

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return ToolResultsQueryReq

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return ToolResultsQueryReq

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
Query persisted Claude tool results using structured filters and optional JMESPath.

Description
-----------
Searches over previously stored tool result records. Useful when the model wants
to inspect which tools ran, in which chat turn, at which step, with what output,
and whether any tool failed.

Important
---------
The actual tool_results dataset is attached internally by the backend at runtime.
The model should only provide filters and query options.

Input
-----
{
    "chat_turn": 4,
    "tool_names": ["search_web", "file_search_tool"],
    "sources": ["custom", "server"],
    "is_error": false,
    "text_query": "invoice",
    "fields": ["tool_use_id", "name", "chat_turn", "step_index", "output_text"],
    "limit": 10,
    "offset": 0
}

Optional advanced input
-----------------------
{
    "chat_turns": [2, 3, 4],
    "jmespath_query": "{name:name, turn:chat_turn, step:step_index}",
    "limit": 20
}

Important fields
----------------
chat_turn / chat_turns / chat_turn_min / chat_turn_max
    Filter by turn number.

step_index / step_indexes / step_index_min / step_index_max
    Filter by internal step/loop inside the turn.

tool_names
    Filter by tool names.

sources
    Filter by source type, usually custom / memory / server.

tool_use_ids
    Filter by exact tool_use_id values.

is_error
    Filter failed or successful tool runs.

text_query
    Case-insensitive substring search across name, output_text, input, meta, and urls.

fields
    Optional projection fields. Supports JMESPath-style field access like:
    ["name", "chat_turn", "meta.block_type"]

jmespath_query
    Optional per-item JMESPath expression applied to each result after filtering,
    sorting, and pagination.

sort_by / descending / limit / offset
    Sorting and pagination controls.

Output
------
{
    "total_count": 12,
    "returned_count": 10,
    "results": [...],
    "summary": {
        "by_name": {"search_web": 5},
        "by_source": {"custom": 8, "server": 4},
        "chat_turns": [2, 3, 4]
    }
}
"""

    @staticmethod
    def attach_tool_results(
        action_config: ToolResultsQueryReq,
        tool_results: list[dict[str, Any]] | None,
    ) -> ToolResultsQueryReq:
        action_config.set_tool_results(tool_results)
        return action_config

    @staticmethod
    def _to_plain_item(item: Any) -> dict[str, Any] | None:
        if hasattr(item, "model_dump"):
            try:
                item = item.model_dump(exclude_none=True)
            except Exception:
                return None

        if isinstance(item, dict):
            return dict(item)

        return None

    @staticmethod
    def _safe_jmespath_search(query: str, data: Any) -> Any:
        try:
            return jmespath.search(query, data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JMESPath query: {str(exc)}")

    @staticmethod
    def _build_text_haystack(item: dict[str, Any]) -> str:
        parts: list[str] = []

        for key in ["tool_use_id", "name", "source", "output_text"]:
            value = item.get(key)
            if value is not None:
                parts.append(str(value))

        for key in ["input", "meta", "urls", "generated_files"]:
            value = item.get(key)
            if value is not None:
                try:
                    parts.append(json.dumps(value, ensure_ascii=False, default=str))
                except Exception:
                    parts.append(str(value))

        return "\n".join(parts).lower()

    @staticmethod
    def _matches_filters(item: dict[str, Any], req: ToolResultsQueryReq) -> bool:
        chat_turn = item.get("chat_turn")
        step_index = item.get("step_index")
        name = str(item.get("name", "")).lower()
        source = str(item.get("source", "")).lower()
        tool_use_id = str(item.get("tool_use_id", ""))
        is_error = item.get("is_error")

        if req.chat_turn is not None and chat_turn != req.chat_turn:
            return False

        if req.chat_turns and chat_turn not in req.chat_turns:
            return False

        if req.chat_turn_min is not None and (chat_turn is None or chat_turn < req.chat_turn_min):
            return False

        if req.chat_turn_max is not None and (chat_turn is None or chat_turn > req.chat_turn_max):
            return False

        if req.step_index is not None and step_index != req.step_index:
            return False

        if req.step_indexes and step_index not in req.step_indexes:
            return False

        if req.step_index_min is not None and (step_index is None or step_index < req.step_index_min):
            return False

        if req.step_index_max is not None and (step_index is None or step_index > req.step_index_max):
            return False

        if req.tool_names:
            allowed_tool_names = {str(x).lower() for x in req.tool_names}
            if name not in allowed_tool_names:
                return False

        if req.sources:
            allowed_sources = {str(x).lower() for x in req.sources}
            if source not in allowed_sources:
                return False

        if req.tool_use_ids:
            allowed_tool_use_ids = {str(x) for x in req.tool_use_ids}
            if tool_use_id not in allowed_tool_use_ids:
                return False

        if req.is_error is not None and bool(is_error) != req.is_error:
            return False

        if req.text_query:
            haystack = ActionToolResultsQueryClaude._build_text_haystack(item)
            if req.text_query.lower() not in haystack:
                return False

        return True

    @staticmethod
    def _sort_items(items: list[dict[str, Any]], req: ToolResultsQueryReq) -> list[dict[str, Any]]:
        if not req.sort_by:
            return items

        def sort_key(item: dict[str, Any]):
            if req.sort_by == "chat_turn":
                return (
                    int(item.get("chat_turn") or -1),
                    int(item.get("step_index") or -1),
                    str(item.get("tool_use_id") or ""),
                )
            if req.sort_by == "step_index":
                return (
                    int(item.get("step_index") or -1),
                    int(item.get("chat_turn") or -1),
                    str(item.get("tool_use_id") or ""),
                )
            if req.sort_by == "name":
                return (
                    str(item.get("name") or ""),
                    int(item.get("chat_turn") or -1),
                    int(item.get("step_index") or -1),
                )
            if req.sort_by == "source":
                return (
                    str(item.get("source") or ""),
                    int(item.get("chat_turn") or -1),
                    int(item.get("step_index") or -1),
                )
            return str(item.get(req.sort_by) or "")

        return sorted(items, key=sort_key, reverse=req.descending)

    @staticmethod
    def _project_item(item: Any, fields: list[str] | None) -> Any:
        if not fields or not isinstance(item, dict):
            return item

        projected: dict[str, Any] = {}
        for field in fields:
            projected[field] = ActionToolResultsQueryClaude._safe_jmespath_search(field, item)
        return projected

    @staticmethod
    def _build_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
        by_name = Counter()
        by_source = Counter()
        chat_turns: set[int] = set()
        error_count = 0

        for item in items:
            by_name[str(item.get("name") or "unknown")] += 1
            by_source[str(item.get("source") or "unknown")] += 1

            if item.get("chat_turn") is not None:
                try:
                    chat_turns.add(int(item["chat_turn"]))
                except Exception:
                    pass

            if bool(item.get("is_error")):
                error_count += 1

        return {
            "by_name": dict(by_name),
            "by_source": dict(by_source),
            "chat_turns": sorted(chat_turns),
            "error_count": error_count,
        }

    @classmethod
    def _execute_query(cls, req: ToolResultsQueryReq) -> ToolResultsQueryRes:
        normalized_items: list[dict[str, Any]] = []
        for raw_item in req.get_tool_results():
            item = cls._to_plain_item(raw_item)
            if item is not None:
                normalized_items.append(item)

        filtered_items = [item for item in normalized_items if cls._matches_filters(item, req)]
        filtered_items = cls._sort_items(filtered_items, req)

        summary = cls._build_summary(filtered_items)

        # JMESPath policy: apply per-item only (after filtering + sorting + pagination).
        total_count = len(filtered_items)
        paged_items = filtered_items[req.offset : req.offset + req.limit]
        results: list[Any] = []
        for item in paged_items:
            working: Any = item
            if req.fields:
                working = cls._project_item(working, req.fields)
            if req.jmespath_query:
                working = cls._safe_jmespath_search(req.jmespath_query, working)
            results.append(working)

        returned_count = len(results)
        return ToolResultsQueryRes(
            total_count=total_count,
            returned_count=returned_count,
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
            logger.bind(ctx=ctx).exception("Error in ActionToolResultsQuery run_action: {}", exc)
            return exc

    async def run_tool(
        self,
        action_config: ToolResultsQueryReq,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> ToolResultsQueryRes | Exception:
        try:
            return self._execute_query(action_config)
        except Exception as exc:
            logger.bind(ctx=ctx).exception("Error in ActionToolResultsQuery run_tool: {}", exc)
            return exc
