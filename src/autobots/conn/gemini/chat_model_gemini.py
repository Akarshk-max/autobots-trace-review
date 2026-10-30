from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.autobots.conn.gemini.chat_memory_tool import GeminiChatMemoryItem
from src.autobots.conn.gemini.model_registry_gemini import (
    GeminiModelRegistry,
    GeminiProvider,
)

GeminiRole = Literal["user", "model"]
GeminiFunctionCallingMode = Literal["AUTO", "VALIDATED", "ANY", "NONE"]
GeminiThinkingLevel = Literal["minimal", "low", "medium", "high"]
GeminiContextStoreScope = Literal["chat_scoped", "named_general_store"]


class GeminiGeneratedFileRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str
    file_name: str
    mime_type: str | None = None
    file_id: str | None = None
    source: Literal["tool_output", "code_execution", "manual_attach"] = "tool_output"
    created_turn: int | None = None
    last_seen_turn: int | None = None


class GeminiUploadedFileRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_url: str
    gemini_file_name: str
    file_uri: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    uploaded_at: str | None = None
    refresh_after_at: str | None = None
    last_checked_at: str | None = None
    gemini_expiration_time: str | None = None
    first_uploaded_turn: int
    last_used_turn: int
    source: Literal["current_turn", "generated", "manual_attach"] = "current_turn"

class GeminiStoredToolResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool_use_id: str
    name: str
    source: Literal["custom", "context", "file_search_store", "code_execution"] = "custom"
    chat_turn: int
    step_index: int
    input: dict[str, Any] = Field(default_factory=dict)
    output_text: str | None = None
    is_error: bool = False
    urls: list[str] = Field(default_factory=list)
    generated_files: list[GeminiGeneratedFileRef | dict[str, Any]] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class GeminiUsageMetadataRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    chat_turn: int
    prompt_token_count: int | None = None
    candidates_token_count: int | None = None
    total_token_count: int | None = None
    thoughts_token_count: int | None = None
    tool_use_prompt_token_count: int | None = None
    cached_content_token_count: int | None = None
    finish_reason: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class GeminiTurnRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    turn: int
    messages: list[dict[str, Any]] = Field(default_factory=list)
    tool_results: list[GeminiStoredToolResult | dict[str, Any]] = Field(default_factory=list)
    usage: GeminiUsageMetadataRecord | dict[str, Any] | None = None


class GeminiContextStoreRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scope: GeminiContextStoreScope = "chat_scoped"
    store_id: str | None = None
    store_name: str | None = None
    display_name: str | None = None
    chat_id: str | None = None
    last_indexed_turn: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

class GeminiThinkingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    thinking_level: GeminiThinkingLevel | None = None
    thinking_budget: int | None = None
    include_thoughts: bool = False


class GeminiToolSelection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    default_tool_names: list[str] = Field(default_factory=list)
    function_calling_mode: GeminiFunctionCallingMode = "AUTO"
    allowed_function_names: list[str] | None = None


class GeminiMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: GeminiRole
    parts: list[dict[str, Any]] = Field(default_factory=list)

GeminiModel = Literal[
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",

    "gemini-3.1-pro-preview",
    "gemini-3.1-pro-preview-customtools",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",

    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",

    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]

class GeminiChatConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: GeminiModel = "gemini-3.5-flash"
    provider: GeminiProvider = GeminiProvider.GEMINI_API

    messages: list[dict[str, Any]] = Field(default_factory=list)
    pinned_messages: list[dict[str, Any]] = Field(default_factory=list)
    turn_history: list[GeminiTurnRecord | dict[str, Any]] = Field(default_factory=list)
    visible_turns: int = Field(default=20, ge=1, le=200)
    chat_turn: int = 0
    chat_id: str | None = None

    system_instruction: str | list[dict[str, Any]] | None = None
    tools: list[str | dict[str, Any]] = Field(default_factory=list)

    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    use_model_default_max_output_tokens: bool = True
    max_output_tokens: int | None = None

    thinking: GeminiThinkingConfig | dict[str, Any] | None = Field(
        default_factory=GeminiThinkingConfig
    )
    tool_selection: GeminiToolSelection | dict[str, Any] = Field(
        default_factory=GeminiToolSelection
    )

    tool_results: list[GeminiStoredToolResult | dict[str, Any]] = Field(default_factory=list)
    tool_results_history: list[GeminiStoredToolResult | dict[str, Any]] = Field(
        default_factory=list
    )
    generated_files: list[GeminiGeneratedFileRef | dict[str, Any]] = Field(default_factory=list)
    current_turn_generated_files: list[GeminiGeneratedFileRef | dict[str, Any]] = Field(default_factory=list)

    current_turn_urls: list[str] = Field(default_factory=list)
    all_turn_urls: list[str] = Field(default_factory=list)
    uploaded_files_manifest: list[GeminiUploadedFileRef | dict[str, Any]] = Field(default_factory=list)


    file_search_store_enabled: bool = True
    file_search_store_ref: dict[str, Any] | None = None
    file_search_store_manifest: list[dict[str, Any]] = Field(default_factory=list)
    file_search_store_auto_index_current_turn_urls: bool = True
    file_search_store_last_indexed_turn: int | None = None

    history_vector_store_enabled: bool = True
    history_vector_store_id: str | None = None
    history_vector_store_name: str | None = None
    history_vector_store_index_every_n_turns: int = Field(default=20, ge=1, le=200)
    history_vector_store_last_indexed_turn: int = 0

    usage_metadata_history: list[GeminiUsageMetadataRecord | dict[str, Any]] = Field(
        default_factory=list
    )
    chat_memory: list[GeminiChatMemoryItem | dict[str, Any]] = Field(default_factory=list)


    @model_validator(mode="after")
    def apply_model_defaults(self) -> "GeminiChatConfig":
        resolved = GeminiModelRegistry.resolve(model=self.model, provider=self.provider)
        caps = resolved.capabilities

        if self.use_model_default_max_output_tokens or self.max_output_tokens is None:
            self.max_output_tokens = caps.output_token_limit
        elif (
            self.max_output_tokens is not None
            and caps.output_token_limit is not None
            and self.max_output_tokens > caps.output_token_limit
        ):
            self.max_output_tokens = caps.output_token_limit

        if not caps.supports_thinking:
            self.thinking = None
        else:
            thinking = self.thinking
            if isinstance(thinking, dict):
                thinking = GeminiThinkingConfig.model_validate(thinking)
            elif thinking is None:
                thinking = GeminiThinkingConfig()

            if caps.family.startswith("gemini-3"):
                if thinking.thinking_level is None:
                    thinking.thinking_level = "medium"
                thinking.thinking_budget = None
            else:
                if thinking.thinking_budget is None:
                    thinking.thinking_budget = -1
                thinking.thinking_level = None
            self.thinking = thinking

        tool_selection = self.tool_selection
        if isinstance(tool_selection, GeminiToolSelection):
            mode = tool_selection.function_calling_mode
        else:
            mode = str(tool_selection.get("function_calling_mode", "AUTO")).upper()

        if not caps.supports_function_calling and mode != "NONE":
            if isinstance(tool_selection, GeminiToolSelection):
                tool_selection.function_calling_mode = "NONE"
            else:
                tool_selection["function_calling_mode"] = "NONE"

        return self

    def resolved_capabilities(self):
        return GeminiModelRegistry.resolve(model=self.model, provider=self.provider).capabilities

    def current_turn(self) -> int:
        return int(self.chat_turn or 1 or 1)

    def next_turn(self) -> int:
        return max(1, self.current_turn() + 1)

    def default_tool_names(self) -> list[str]:
        selection = self.tool_selection
        if isinstance(selection, GeminiToolSelection):
            return list(selection.default_tool_names)
        return list(selection.get("default_tool_names", []) or [])

    def function_calling_mode(self) -> GeminiFunctionCallingMode:
        selection = self.tool_selection
        if isinstance(selection, GeminiToolSelection):
            return selection.function_calling_mode
        value = str(selection.get("function_calling_mode", "AUTO")).upper()
        if value not in {"AUTO", "VALIDATED", "ANY", "NONE"}:
            return "AUTO"
        return value  # type: ignore[return-value]

    def allowed_function_names(self) -> list[str] | None:
        selection = self.tool_selection
        if isinstance(selection, GeminiToolSelection):
            return selection.allowed_function_names
        names = selection.get("allowed_function_names")
        return list(names) if isinstance(names, list) else None
