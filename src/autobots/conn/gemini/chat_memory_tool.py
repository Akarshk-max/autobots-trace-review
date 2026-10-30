from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.autobots.data_model.context import Context

GeminiChatMemoryKind = Literal[
    "goal",
    "decision",
    "fact",
    "preference",
    "constraint",
    "open_question",
    "note",
]
GeminiChatMemoryImportance = Literal["low", "medium", "high"]
GeminiChatMemoryAction = Literal["put", "get", "list", "delete"]


class GeminiChatMemoryItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    kind: GeminiChatMemoryKind
    key: str
    value: str
    importance: GeminiChatMemoryImportance = "medium"
    tags: list[str] = Field(default_factory=list)
    source_turn: int | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @model_validator(mode="after")
    def normalize(self) -> "GeminiChatMemoryItem":
        self.key = str(self.key or "").strip()
        self.value = str(self.value or "").strip()
        self.tags = GeminiChatMemoryToolService.normalize_tags(self.tags)
        return self


class GeminiChatMemoryToolReq(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: GeminiChatMemoryAction
    kind: GeminiChatMemoryKind | None = None
    key: str | None = None
    value: str | None = None
    importance: GeminiChatMemoryImportance = "medium"
    tags: list[str] = Field(default_factory=list)
    query: str | None = None
    limit: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def validate_fields(self) -> "GeminiChatMemoryToolReq":
        if self.action == "put":
            if not str(self.key or "").strip():
                raise ValueError("key is required for put")
            if not str(self.value or "").strip():
                raise ValueError("value is required for put")
            if self.kind is None:
                raise ValueError("kind is required for put")
        elif self.action in {"get", "delete"}:
            if not str(self.key or "").strip():
                raise ValueError(f"key is required for {self.action}")
        return self


class GeminiChatMemoryToolRes(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: GeminiChatMemoryAction
    status: Literal["ok", "not_found"] = "ok"
    message: str
    item: GeminiChatMemoryItem | None = None
    items: list[GeminiChatMemoryItem] = Field(default_factory=list)
    count: int = 0


class GeminiChatMemoryToolService:
    IMPORTANCE_RANK: dict[str, int] = {"high": 3, "medium": 2, "low": 1}

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def normalize_tags(tags: list[str] | None) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for tag in tags or []:
            cleaned = str(tag or "").strip().lower()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                normalized.append(cleaned)
        return normalized

    @classmethod
    def _coerce_items(
        cls,
        chat_memory: list[GeminiChatMemoryItem | dict[str, Any]] | None,
    ) -> list[GeminiChatMemoryItem]:
        items: list[GeminiChatMemoryItem] = []
        for item in chat_memory or []:
            if isinstance(item, GeminiChatMemoryItem):
                items.append(item)
            elif isinstance(item, dict):
                items.append(GeminiChatMemoryItem.model_validate(item))
        return items

    @classmethod
    def _sort_items(cls, items: list[GeminiChatMemoryItem]) -> list[GeminiChatMemoryItem]:
        def sort_key(item: GeminiChatMemoryItem) -> tuple[int, str, str]:
            return (
                cls.IMPORTANCE_RANK.get(str(item.importance), 0),
                str(item.updated_at or ""),
                str(item.created_at or ""),
            )

        return sorted(items, key=sort_key, reverse=True)

    @classmethod
    def _matches(cls, item: GeminiChatMemoryItem, req: GeminiChatMemoryToolReq) -> bool:
        if req.kind and item.kind != req.kind:
            return False
        query = str(req.query or "").strip().lower()
        if not query:
            return True
        haystacks = [item.key.lower(), item.value.lower(), item.kind.lower()]
        haystacks.extend(tag.lower() for tag in item.tags)
        return any(query in hay for hay in haystacks)

    @classmethod
    def execute(
        cls,
        req: GeminiChatMemoryToolReq,
        *,
        chat_memory: list[GeminiChatMemoryItem | dict[str, Any]] | None,
        current_turn: int | None,
    ) -> tuple[GeminiChatMemoryToolRes, list[dict[str, Any]]]:
        items = cls._coerce_items(chat_memory)
        now = cls.now_iso()
        key = str(req.key or "").strip()

        if req.action == "put":
            existing = next((item for item in items if item.key == key), None)
            if existing is None:
                item = GeminiChatMemoryItem(
                    id=f"chat-memory-{len(items) + 1}",
                    kind=req.kind,
                    key=key,
                    value=str(req.value or "").strip(),
                    importance=req.importance,
                    tags=cls.normalize_tags(req.tags),
                    source_turn=current_turn,
                    created_at=now,
                    updated_at=now,
                )
                items.append(item)
                message = f"Stored {item.kind} '{item.key}'."
            else:
                existing.kind = req.kind or existing.kind
                existing.value = str(req.value or "").strip()
                existing.importance = req.importance
                existing.tags = cls.normalize_tags(req.tags) or existing.tags
                existing.source_turn = current_turn
                existing.updated_at = now
                item = existing
                message = f"Updated {item.kind} '{item.key}'."
            ordered = cls._sort_items(items)
            return (
                GeminiChatMemoryToolRes(
                    action=req.action,
                    status="ok",
                    message=message,
                    item=item,
                    count=1,
                ),
                [entry.model_dump(exclude_none=True) for entry in ordered],
            )

        if req.action == "get":
            item = next((entry for entry in items if entry.key == key), None)
            if item is None:
                return (
                    GeminiChatMemoryToolRes(
                        action=req.action,
                        status="not_found",
                        message=f"No chat memory found for key '{key}'.",
                        count=0,
                    ),
                    [entry.model_dump(exclude_none=True) for entry in cls._sort_items(items)],
                )
            return (
                GeminiChatMemoryToolRes(
                    action=req.action,
                    status="ok",
                    message=f"Found chat memory for key '{key}'.",
                    item=item,
                    count=1,
                ),
                [entry.model_dump(exclude_none=True) for entry in cls._sort_items(items)],
            )

        if req.action == "delete":
            remaining = [entry for entry in items if entry.key != key]
            if len(remaining) == len(items):
                return (
                    GeminiChatMemoryToolRes(
                        action=req.action,
                        status="not_found",
                        message=f"No chat memory found for key '{key}'.",
                        count=0,
                    ),
                    [entry.model_dump(exclude_none=True) for entry in cls._sort_items(items)],
                )
            ordered = cls._sort_items(remaining)
            return (
                GeminiChatMemoryToolRes(
                    action=req.action,
                    status="ok",
                    message=f"Deleted chat memory for key '{key}'.",
                    count=0,
                ),
                [entry.model_dump(exclude_none=True) for entry in ordered],
            )

        filtered = [entry for entry in cls._sort_items(items) if cls._matches(entry, req)]
        limited = filtered[: req.limit]
        return (
            GeminiChatMemoryToolRes(
                action=req.action,
                status="ok",
                message=f"Returned {len(limited)} chat memory item(s).",
                items=limited,
                count=len(limited),
            ),
            [entry.model_dump(exclude_none=True) for entry in cls._sort_items(items)],
        )


async def gemini_chat_memory_tool(
    config: GeminiChatMemoryToolReq,
    ctx: Context,
) -> GeminiChatMemoryToolRes:
    """
    Gemini chat-scoped memory tool.

    Use this tool to store and retrieve compact high-value memory during the current Gemini chat.
    This is for quick lookup of things the model wants to remember without replaying large history.

    Memory kinds to store:
    - goal
    - decision
    - fact
    - preference
    - constraint
    - open_question
    - note

    Supported actions:
    - put: create or update a memory item by key
    - get: fetch one memory item by exact key
    - list: list memory items, optionally filtered by kind or query
    - delete: remove one memory item by exact key

    Guidance:
    - Keep keys short and stable.
    - Keep values concise and high-signal.
    - Prefer storing durable facts, decisions, goals, user preferences, and constraints.
    - Do not store large raw logs or giant tool payloads.

    This tool is intended to be executed inside GeminiChatRuntime, where chat-scoped memory is available.
    """
    return GeminiChatMemoryToolRes(
        action=config.action,
        status="not_found",
        message="gemini_chat_memory_tool must run inside GeminiChatRuntime.",
        count=0,
    )
