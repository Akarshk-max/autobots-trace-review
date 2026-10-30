from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

GeminiContextToolOperation = Literal[
    "get_turns",
    "get_tool_results",
    "stats",
]
GeminiContextSearchSource = Literal["turn_history", "tool_results", "chat_store"]
GeminiContextMessageRole = Literal["user", "model", "assistant"]
GeminiContextSortBy = Literal["score", "turn", "tool_name", "source_type"]


class GeminiContextQueryReq(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: GeminiContextToolOperation = "query"
    search_sources: list[GeminiContextSearchSource] = Field(
        default_factory=lambda: ["turn_history", "tool_results"]
    )
    top_k: int = Field(default=8, ge=1, le=20)

    turn: int | None = None
    turns: list[int] | None = None
    turn_min: int | None = None
    turn_max: int | None = None

    roles: list[GeminiContextMessageRole] | None = None
    return_full_turn: bool = True

    tool_names: list[str] | None = None
    include_error_results: bool = True
    has_generated_files: bool | None = None
    has_urls: bool | None = None

    include_pinned_messages: bool = False
    include_generated_files: bool = True
    include_urls: bool = True

    metadata_filter: str | None = None
    fields: list[str] | None = None

    sort_by: GeminiContextSortBy = "score"
    descending: bool = True
    exact_match_only: bool = False

    limit: int = Field(default=10, ge=1, le=20)
    offset: int = Field(default=0, ge=0)

    semantic_query: str | None = None
    semantic_max_results: int = Field(default=5, ge=1, le=20)

    _turn_history: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _tool_results: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _pinned_messages: list[dict[str, Any]] = PrivateAttr(default_factory=list)

    @model_validator(mode="after")
    def validate_request(self) -> "GeminiContextQueryReq":
        if self.turns is not None and len(self.turns) > 20:
            raise ValueError("turns list too large: max 20 per tool call")


        if self.operation in { "get_turns", "get_tool_results" } and not any(
            source in {"turn_history", "tool_results", "chat_store"}
            for source in (self.search_sources or [])
        ):
            raise ValueError("search_sources must include at least one supported source")

        return self

    def set_runtime_context(
        self,
        *,
        turn_history: list[dict[str, Any]] | None,
        tool_results: list[dict[str, Any]] | None,
        pinned_messages: list[dict[str, Any]] | None = None,
    ) -> "GeminiContextQueryReq":
        self._turn_history = [dict(item) for item in (turn_history or []) if isinstance(item, dict)]
        self._tool_results = [dict(item) for item in (tool_results or []) if isinstance(item, dict)]
        self._pinned_messages = [dict(item) for item in (pinned_messages or []) if isinstance(item, dict)]
        return self

    def get_turn_history(self) -> list[dict[str, Any]]:
        return list(self._turn_history)

    def get_tool_results(self) -> list[dict[str, Any]]:
        return list(self._tool_results)

    def get_pinned_messages(self) -> list[dict[str, Any]]:
        return list(self._pinned_messages)

class GeminiContextMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: GeminiContextSearchSource
    score: float = 0.0
    title: str | None = None
    snippet: str | None = None
    turn: int | None = None
    tool_name: str | None = None
    record_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class GeminiContextToolStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_history_count: int = 0
    tool_results_count: int = 0
    pinned_messages_count: int = 0
    chat_store_available: bool = False
    chat_store_last_indexed_turn: int | None = None
    chat_store_ref: dict[str, Any] | None = None


class GeminiContextQueryRes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    operation: GeminiContextToolOperation
    summary: str | None = None
    total_count: int = 0
    returned_count: int = 0
    matches: list[GeminiContextMatch | dict[str, Any]] = Field(default_factory=list)
    stats: GeminiContextToolStats | dict[str, Any] = Field(default_factory=GeminiContextToolStats)
    meta: dict[str, Any] = Field(default_factory=dict)
