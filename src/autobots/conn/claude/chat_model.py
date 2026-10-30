import re
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, TypeAlias, Union

from anthropic.types import (
    DocumentBlockParam,
    ImageBlockParam,
    MetadataParam,
    RedactedThinkingBlockParam,
    ServerToolUseBlockParam,
    TextBlockParam,
    ThinkingBlockParam,
    ThinkingConfigParam,
    ToolChoiceParam,
    ToolResultBlockParam,
    ToolUnionParam,
    ToolUseBlockParam,
    WebSearchResultBlockParam,
)
from anthropic.types.beta import (
    BetaBashCodeExecutionToolResultBlockParam,
    BetaCodeExecutionToolResultBlockParam,
    BetaContainerUploadBlockParam,
    BetaRedactedThinkingBlockParam,
    BetaRequestMCPServerURLDefinitionParam,
    BetaServerToolUseBlockParam,
    BetaTextBlockParam,
    BetaTextEditorCodeExecutionToolResultBlockParam,
    BetaThinkingBlockParam,
    BetaToolChoiceParam,
    BetaToolResultBlockParam,
    BetaToolUnionParam,
    BetaToolUseBlockParam,
    BetaWebFetchToolResultBlockParam,
    BetaWebSearchToolResultBlockParam,
)
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import TypedDict

from src.autobots.conn.claude.context_management import (
    ClaudeContextManagementConfig,
    ClaudeContextManagementPolicy,
)
from src.autobots.conn.claude.model_registry import ClaudeModelRegistry, ClaudeProvider
from src.autobots.conn.claude.prompt_caching import ClaudeCacheControl, ClaudePromptCachingConfig
from src.autobots.conn.claude.token_counting import ClaudeTokenCountPreflightConfig


class ClaudeModelEnum(str, Enum):
    CLAUDE_FABLE_5_1 = "claude-fable-5-1"
    CLAUDE_FABLE_5 = "claude-fable-5"
    CLAUDE_OPUS_5 = "claude-opus-5"
    CLAUDE_OPUS_4_8 = "claude-opus-4-8"
    CLAUDE_OPUS_4_7 = "claude-opus-4-7"
    CLAUDE_OPUS_4_6 = "claude-opus-4-6"
    CLAUDE_SONNET_4_6 = "claude-sonnet-4-6"
    CLAUDE_SONNET_5 = "claude-sonnet-5"
    CLAUDE_HAIKU_4_5 = "claude-haiku-4-5"
    CLAUDE_SONNET_4_5 = "claude-sonnet-4-5-20250929"
    CLAUDE_OPUS_4_5 = "claude-opus-4-5-20251101"

    @classmethod
    def get_models_dict(cls) -> Dict[str, str]:
        return {item.name: item.value for item in cls}

    @classmethod
    def get_models_list(cls) -> List[str]:
        return [item.value for item in cls]


ClaudeModelParam: TypeAlias = Union[
    Literal[
        "claude-fable-5-1",
        "claude-fable-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "claude-sonnet-4-5-20250929",
        "claude-opus-4-5-20251101",
    ],
    str,
]


class ClaudeGeneratedFileRef(BaseModel):
    s3_url: str
    file_id: str
    file_name: str


class ClaudeUploadedFileRef(BaseModel):
    source_url: str
    claude_file_id: str
    file_name: str | None = None
    first_uploaded_turn: int
    last_used_turn: int
    source: Literal["current_turn", "generated", "manual_attach"] = "manual_attach"


class ClaudeContainerAttachment(BaseModel):
    container_id: str
    source_url: str
    claude_file_id: str
    file_name: str | None = None
    attached_turn: int
    source: Literal["current_turn", "generated", "manual_attach"] = "manual_attach"


class ClaudeStoredToolResult(BaseModel):
    tool_use_id: str
    name: str
    source: str
    chat_turn: int
    step_index: int
    input: Dict[str, Any] = Field(default_factory=dict)
    output_text: str | None = None
    is_error: bool = False
    urls: List[str] = Field(default_factory=list)
    generated_files: List[ClaudeGeneratedFileRef | Dict[str, Any]] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)


class ClaudeToolResultEnvelope(TypedDict):
    tool_result_block: dict[str, Any]
    extra_user_content_blocks: list[dict[str, Any]]
    persisted_tool_result: ClaudeStoredToolResult | Dict[str, Any] | None


class ClaudeVectorStoreTrackedFile(BaseModel):
    source_url: str
    prepared_url: str | None = None
    display_name: str | None = None
    source: Literal["user_input", "tool_output", "generated", "converted"] = "user_input"

    first_seen_turn: int
    last_seen_turn: int

    representation: Literal[
        "native_vector_store",
        "converted_txt_vector_store",
        "container_only",
        "image_only",
    ] = "native_vector_store"

    status: Literal[
        "discovered", "prepared", "pending", "indexed", "failed", "unsupported", "stale"
    ] = "discovered"

    source_content_sha256: str | None = None
    openai_file_id: str | None = None
    vector_store_file_id: str | None = None
    last_error: str | None = None
    last_indexed_turn: int | None = None


class ClaudeMessageParam(TypedDict, total=False):
    content: Union[
        str,
        List[
            Union[
                TextBlockParam,
                ImageBlockParam,
                ToolUseBlockParam,
                ToolResultBlockParam,
                BetaToolUseBlockParam,
                DocumentBlockParam,
                ThinkingBlockParam,
                RedactedThinkingBlockParam,
                WebSearchResultBlockParam,
                BetaWebFetchToolResultBlockParam,
                BetaWebSearchToolResultBlockParam,
                BetaTextBlockParam,
                BetaThinkingBlockParam,
                BetaToolResultBlockParam,
                BetaRedactedThinkingBlockParam,
                BetaServerToolUseBlockParam,
                BetaTextEditorCodeExecutionToolResultBlockParam,
                BetaBashCodeExecutionToolResultBlockParam,
                BetaCodeExecutionToolResultBlockParam,
                ServerToolUseBlockParam,
                BetaContainerUploadBlockParam,
                Dict[str, Any],
            ]
        ],
    ]
    role: Literal["user", "assistant", ""]


class ClaudeTurnRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn: int
    messages: List[ClaudeMessageParam]


class ChatReqClaude(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    max_tokens: int = 64000
    messages: List[ClaudeMessageParam]

    # Conversation-history controls
    visible_turns: int = Field(default=20, ge=1, le=200)
    pinned_messages: List[ClaudeMessageParam] = Field(default_factory=list)
    turn_history: List[ClaudeTurnRecord | Dict[str, Any]] = Field(default_factory=list)

    model: ClaudeModelParam = "claude-opus-4-6"
    provider: ClaudeProvider = ClaudeProvider.ANTHROPIC

    metadata: MetadataParam | None = None
    stop_sequences: List[str] | None = None
    stream: Literal[False] | Literal[True] | None = None
    system: Union[str, List[TextBlockParam | Dict[str, Any]]] | None = None
    temperature: float | None = 0.8
    citations: bool | None = Field(default=True)
    output_config: Dict[str, Any] | None = None

    thinking: Optional[ThinkingConfigParam] | None = None
    thinking_mode: Optional[Literal["standard", "extended"]] | None = None
    thinking_budget: Optional[int] | None = None
    thinking_effort: Optional[Literal["low", "medium", "high", "xhigh", "max"]] | None = None
    max_thoughts: Optional[int] | None = None

    tool_choice: ToolChoiceParam | BetaToolChoiceParam | None = None
    tools: list[ToolUnionParam | BetaToolUnionParam | dict[str, Any] | str] | None = None
    generated_files: List[ClaudeGeneratedFileRef | Dict[str, Any]] = Field(default_factory=list)

    mcp_servers: List[BetaRequestMCPServerURLDefinitionParam] | None = None
    betas: list[str] | None = None

    cache_control: ClaudeCacheControl | None = None
    prompt_caching: ClaudePromptCachingConfig | None = None
    token_count_preflight: ClaudeTokenCountPreflightConfig | None = None

    context_management: ClaudeContextManagementConfig | None = None
    context_management_policy: ClaudeContextManagementPolicy | None = None

    top_k: int | None = None
    top_p: float | None = None
    timeout: float | None = 180
    computer_use: bool | None = Field(default=None)
    container: str | None = Field(default=None)
    auto_resume_paused_compaction: bool = True
    chat_turn: int = 0

    tool_results: List[ClaudeStoredToolResult | Dict[str, Any]] = Field(default_factory=list)
    tool_results_history: List[ClaudeStoredToolResult | Dict[str, Any]] = Field(
        default_factory=list
    )

    vector_store_enabled: bool = True
    vector_store_id: str | None = None
    vector_store_name: str | None = None

    history_vector_store_enabled: bool = True
    history_vector_store_id: str | None = None
    history_vector_store_name: str | None = None
    history_vector_store_index_every_n_turns: int = Field(default=20, ge=1, le=200)
    history_vector_store_last_indexed_turn: int = 0

    current_turn_urls: List[str] = Field(default_factory=list)
    current_turn_vector_store_urls: List[str] = Field(default_factory=list)
    current_turn_container_urls: List[str] = Field(default_factory=list)
    all_turn_urls: List[str] = Field(default_factory=list)

    vector_store_manifest: List[ClaudeVectorStoreTrackedFile | Dict[str, Any]] = Field(
        default_factory=list
    )

    container_candidate_urls: List[str] = Field(default_factory=list)
    uploaded_files_manifest: List[ClaudeUploadedFileRef | Dict[str, Any]] = Field(
        default_factory=list
    )
    container_available_files: List[ClaudeContainerAttachment | Dict[str, Any]] = Field(
        default_factory=list
    )

    @field_validator("thinking", mode="before")
    @classmethod
    def normalize_thinking_config(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value

        if "type" in value:
            return value

        if "include_thoughts" not in value:
            return value

        if not value.get("include_thoughts"):
            return {"type": "disabled"}

        return {
            "type": "enabled",
            "budget_tokens": value.get("budget_tokens")
            or value.get("thinking_budget")
            or value.get("max_thoughts")
            or 2048,
        }

    @staticmethod
    def _is_opus_47_or_later(model: str) -> bool:
        m = re.match(r"^claude-opus-4-(\d{1,2})(?:-|$)", str(model))
        if not m:
            return False
        try:
            minor = int(m.group(1))
        except ValueError:
            return False
        return minor >= 7

    def _thinking_type(self) -> str | None:
        if isinstance(self.thinking, dict):
            return str(self.thinking.get("type"))
        return None

    def _tools_present(self) -> bool:
        return bool(self.tools)

    def _interleaved_thinking_effective(self) -> bool:
        thinking_type = self._thinking_type()
        if thinking_type not in {"enabled", "adaptive"}:
            return False

        if not self._tools_present():
            return False

        model = str(self.model)
        betas = set(self.betas or [])

        if thinking_type == "adaptive" and (
            model.startswith("claude-opus-4-6") or model.startswith("claude-sonnet-4-6")
        ):
            return True

        return "interleaved-thinking-2025-05-14" in betas

    @model_validator(mode="after")
    def apply_thinking_defaults(self) -> "ChatReqClaude":
        resolved = ClaudeModelRegistry.resolve(str(self.model), self.provider)
        caps = resolved.capabilities
        always_on_adaptive = getattr(caps, "always_on_adaptive_thinking", False)

        def _legacy_thinking_config() -> dict[str, Any]:
            return {
                "type": "enabled",
                "budget_tokens": self.thinking_budget or self.max_thoughts or 2048,
            }

        def _drop_output_effort_if_unsupported() -> None:
            if getattr(caps, "supports_effort", False):
                return

            if isinstance(self.output_config, dict) and "effort" in self.output_config:
                logger.info(
                    "Removing output_config.effort during validation because model={} provider={} does not support effort",
                    self.model,
                    self.provider,
                )
                self.output_config.pop("effort", None)

                if not self.output_config:
                    self.output_config = None

        # this is now safe for fable5 and other legacy models also
        if self.thinking_mode == "standard":
            if always_on_adaptive:
                logger.info(
                    "Ignoring thinking_mode=standard because model={} has always-on adaptive thinking",
                    self.model,
                )
                self.thinking = None
            else:
                self.thinking = {"type": "disabled"}

        elif self.thinking_mode == "extended" and self.thinking is None:
            if self.thinking_budget is None and self.max_thoughts is not None:
                self.thinking_budget = self.max_thoughts

            if caps.supports_adaptive_thinking:
                self.thinking = {"type": "adaptive"}

                if getattr(caps, "supports_effort", False):
                    if self.output_config is None:
                        self.output_config = {}
                    self.output_config.setdefault("effort", self.thinking_effort or "medium")
            elif caps.supports_thinking:
                self.thinking = _legacy_thinking_config()
            else:
                logger.info(
                    "Disabling thinking because model={} provider={} does not support thinking",
                    self.model,
                    self.provider,
                )
                self.thinking = {"type": "disabled"}

        elif isinstance(self.thinking, dict) and self.thinking.get("type") == "adaptive":
            logger.debug("Adaptive thinking config requested model={}", self.model)

            if not caps.supports_adaptive_thinking:
                if caps.supports_thinking:
                    logger.info(
                        "Converting unsupported adaptive thinking to legacy enabled thinking during validation "
                        "model={} provider={}",
                        self.model,
                        self.provider,
                    )
                    self.thinking = _legacy_thinking_config()
                else:
                    logger.info(
                        "Disabling adaptive thinking during validation because model={} provider={} does not support thinking",
                        self.model,
                        self.provider,
                    )
                    self.thinking = {"type": "disabled"}

            elif self.thinking_effort is not None and getattr(caps, "supports_effort", False):
                if self.output_config is None:
                    self.output_config = {}
                self.output_config.setdefault("effort", self.thinking_effort)

        elif isinstance(self.thinking, dict) and self.thinking.get("type") == "enabled":
            logger.debug("Legacy thinking config enabled model={}", self.model)

            if not caps.supports_thinking:
                logger.info(
                    "Disabling legacy thinking because model={} provider={} does not support thinking",
                    self.model,
                    self.provider,
                )
                self.thinking = {"type": "disabled"}

        if always_on_adaptive:
            if isinstance(self.thinking, dict) and self.thinking.get("type") == "disabled":
                logger.info(
                    "Dropping thinking.type=disabled because model={} has always-on adaptive thinking",
                    self.model,
                )
                self.thinking = None

            if self.thinking_effort is not None and getattr(caps, "supports_effort", False):
                if self.output_config is None:
                    self.output_config = {}
                self.output_config["effort"] = self.thinking_effort

        _drop_output_effort_if_unsupported()

        if self.mcp_servers:
            self.betas = list(dict.fromkeys((self.betas or []) + ["mcp-client-2025-11-20"]))

        return self

    @staticmethod
    def _content_is_non_empty(content: Any) -> bool:
        if content is None:
            return False

        if isinstance(content, str):
            return bool(content.strip())

        if isinstance(content, list):
            if not content:
                return False
            for raw_block in content:
                if isinstance(raw_block, str):
                    if raw_block.strip():
                        return True
                    continue
                if not isinstance(raw_block, dict):
                    # Defensive: unknown block types should count as content.
                    return True
                block_type = str(raw_block.get("type") or "")
                if block_type == "text":
                    if str(raw_block.get("text") or "").strip():
                        return True
                    continue
                # Non-text blocks (tool_result, image, document, etc.) count as content.
                return True
            return False

        # Unknown content types: treat as non-empty to avoid accidental drops.
        return True

    def _drop_empty_user_messages(self) -> None:
        if not self.messages:
            self.messages = [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "Explain about your capabilities in brief and ask user to what user wants to do."}],
                }
            ]
            return

        before = len(self.messages)
        cleaned: list[Any] = []
        dropped = 0
        for raw_msg in self.messages:
            if not isinstance(raw_msg, dict):
                cleaned.append(raw_msg)
                continue
            if raw_msg.get("role") == "user" and not self._content_is_non_empty(raw_msg.get("content")):
                dropped += 1
                continue
            cleaned.append(raw_msg)

        # Ensure at least one non-empty user message exists after pruning.
        has_user = False
        for msg in cleaned:
            if isinstance(msg, dict) and msg.get("role") == "user" and self._content_is_non_empty(msg.get("content")):
                has_user = True
                break
        if not has_user:
            cleaned.append(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "Explain about your capabilities in brief and ask user to what user wants to do."}],
                }
            )

        if dropped:
            logger.warning(
                "Dropped {} empty user messages before sending request to Claude (before={} after={})",
                dropped,
                before,
                len(cleaned),
            )
        self.messages = cleaned


    def sanitize_for_model(self) -> None:
        model = str(self.model)
        resolved = ClaudeModelRegistry.resolve(model, self.provider)
        caps = resolved.capabilities
        
        # for 4.7 or later models
        always_on_adaptive = getattr(caps, "always_on_adaptive_thinking", False)
        rejects_sampling_params = (
            getattr(caps, "rejects_sampling_params", False)
            or self._is_opus_47_or_later(model)
        )

        # Anthropic rejects user messages with empty content; sanitize defensively.
        self._drop_empty_user_messages()
        if always_on_adaptive:
            if isinstance(self.thinking, dict) and self.thinking.get("type") == "disabled":
                logger.info(
                    "Dropping thinking.type=disabled because model={} has always-on adaptive thinking",
                    self.model,
                )
                self.thinking = None

        if getattr(caps, "rejects_forced_tool_choice", False) and isinstance(self.tool_choice, dict):
            if self.tool_choice.get("type") in ("any", "tool"):
                logger.warning(
                    "Dropping tool_choice={} because model={} rejects forced tool use",
                    self.tool_choice,
                    self.model,
                )
                self.tool_choice = {"type": "auto"}

        def _legacy_thinking_config() -> dict[str, Any]:
            return {
                "type": "enabled",
                "budget_tokens": self.thinking_budget or self.max_thoughts or 2048,
            }

        def _drop_output_effort_if_unsupported() -> None:
            if getattr(caps, "supports_effort", False):
                return

            if isinstance(self.output_config, dict) and "effort" in self.output_config:
                logger.info(
                    "Removing output_config.effort because model={} provider={} does not support effort",
                    self.model,
                    self.provider,
                )
                self.output_config.pop("effort", None)

                if not self.output_config:
                    self.output_config = None

        def _ensure_output_effort_if_supported(default_effort: str = "medium") -> None:
            if not getattr(caps, "supports_effort", False):
                return

            if self.output_config is None:
                self.output_config = {}

            if self.thinking_effort is not None:
                self.output_config["effort"] = self.thinking_effort
            else:
                self.output_config.setdefault("effort", default_effort)

        # First remove unsupported effort. This protects against stale client payloads.
        _drop_output_effort_if_unsupported()

        # Normalize unsupported thinking configs before sending request.
        if isinstance(self.thinking, dict):
            thinking_type = self.thinking.get("type")

            if thinking_type == "adaptive" and not caps.supports_adaptive_thinking:
                if caps.supports_thinking:
                    logger.info(
                        "Converting unsupported adaptive thinking to legacy enabled thinking for model={} provider={}",
                        self.model,
                        self.provider,
                    )
                    self.thinking = _legacy_thinking_config()
                else:
                    logger.info(
                        "Disabling adaptive thinking because model={} provider={} does not support thinking",
                        self.model,
                        self.provider,
                    )
                    self.thinking = {"type": "disabled"}

            elif thinking_type == "enabled" and not caps.supports_thinking:
                logger.info(
                    "Disabling legacy thinking because model={} provider={} does not support thinking",
                    self.model,
                    self.provider,
                )
                self.thinking = {"type": "disabled"}

        is_opus_47_or_later = rejects_sampling_params

        if caps.max_output_tokens is not None and self.max_tokens > caps.max_output_tokens:
            logger.info(
                "Capping max_tokens model={} from {} to {}",
                self.model,
                self.max_tokens,
                caps.max_output_tokens,
            )
            self.max_tokens = caps.max_output_tokens

        if (
            caps.rejects_assistant_prefill
            and self.messages
            and self.messages[-1].get("role") == "assistant"
        ):
            logger.warning(
                "Removing final assistant message due to prefill restriction model={}", self.model
            )
            self.messages.pop()

        # Claude Opus 4.7+ rejects sampling params and legacy manual thinking budgets.
        if is_opus_47_or_later:
            if self.temperature is not None:
                logger.info("Removing temperature because model={} does not support it", self.model)
                self.temperature = None

            if self.top_p is not None:
                logger.info("Removing top_p because model={} does not support it", self.model)
                self.top_p = None

            if self.top_k is not None:
                logger.info("Removing top_k because model={} does not support it", self.model)
                self.top_k = None

            if isinstance(self.thinking, dict):
                thinking_type = self.thinking.get("type")

                if thinking_type == "enabled":
                    logger.info(
                        "Converting thinking type=enabled to adaptive for model={}", self.model
                    )
                    self.thinking = {"type": "adaptive"}

                elif thinking_type == "adaptive":
                    self.thinking.pop("budget_tokens", None)

            elif self.thinking_mode == "extended":
                self.thinking = {"type": "adaptive"}

            if self.thinking and isinstance(self.thinking, dict):
                self.thinking.pop("budget_tokens", None)

            if (
                self.thinking
                and isinstance(self.thinking, dict)
                and self.thinking.get("type") == "adaptive"
            ):
                _ensure_output_effort_if_supported(default_effort="high")

            if always_on_adaptive and self.thinking_effort is not None:
                _ensure_output_effort_if_supported(default_effort="high")

            _drop_output_effort_if_unsupported()

            if self.betas:
                self.betas = list(dict.fromkeys(self.betas))

            return

        # Re-check after possible Opus 4.7 branch bypass.
        _drop_output_effort_if_unsupported()

        thinking_type = self._thinking_type()
        thinking_enabled = thinking_type in {"enabled", "adaptive"}
        interleaved_effective = self._interleaved_thinking_effective()

        if thinking_enabled:
            if self.top_k is not None:
                logger.info("Removing top_k because thinking is enabled model={}", self.model)
                self.top_k = None

            if self.temperature is not None:
                logger.info("Removing temperature because thinking is enabled model={}", self.model)
                self.temperature = None

            if isinstance(self.thinking, dict):
                budget = self.thinking.get("budget_tokens")

                # Adaptive thinking should not carry legacy manual budget_tokens.
                if thinking_type == "adaptive":
                    self.thinking.pop("budget_tokens", None)

                    if self.thinking_effort is not None:
                        _ensure_output_effort_if_supported(default_effort="medium")
                    else:
                        _drop_output_effort_if_unsupported()

                elif thinking_type == "enabled":
                    if not isinstance(budget, int):
                        budget = self.thinking_budget or self.max_thoughts or 2048

                    min_budget = 1024

                    if budget < min_budget:
                        logger.info(
                            "Raising thinking budget_tokens from {} to {}", budget, min_budget
                        )
                        budget = min_budget

                    if not interleaved_effective:
                        if self.max_tokens <= min_budget:
                            logger.warning(
                                "Disabling thinking because max_tokens={} is too small for the minimum thinking budget",
                                self.max_tokens,
                            )
                            self.thinking = {"type": "disabled"}
                        else:
                            if budget >= self.max_tokens:
                                budget = self.max_tokens - 1
                                logger.info("Capping thinking budget_tokens to {}", budget)

                        if (
                            isinstance(self.thinking, dict)
                            and self.thinking.get("type") == "enabled"
                        ):
                            self.thinking["budget_tokens"] = budget
                    else:
                        logger.debug(
                            "Allowing thinking budget_tokens={} to exceed max_tokens={} because "
                            "interleaved thinking is effective",
                            budget,
                            self.max_tokens,
                        )
                        if self.thinking.get("type") == "enabled":
                            self.thinking["budget_tokens"] = budget

            if self.top_p is not None and not (0.95 <= self.top_p <= 1.0):
                logger.info("Removing incompatible top_p={} for thinking mode", self.top_p)
                self.top_p = None

        if self.temperature is not None and self.top_p is not None:
            logger.info("Both temperature and top_p set. Dropping top_p.")
            self.top_p = None

        _drop_output_effort_if_unsupported()

        if self.betas:
            self.betas = list(dict.fromkeys(self.betas))
