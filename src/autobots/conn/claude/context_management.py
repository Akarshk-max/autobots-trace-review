from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from src.autobots.conn.claude.model_registry import ClaudeModelRegistry


class InputTokensTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["input_tokens"] = "input_tokens"
    value: int


class ToolUsesTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_uses"] = "tool_uses"
    value: int


class KeepThinkingTurns(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["thinking_turns"] = "thinking_turns"
    value: int


class KeepToolUses(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_uses"] = "tool_uses"
    value: int


class ClearAtLeastInputTokens(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["input_tokens"] = "input_tokens"
    value: int


class CompactEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["compact_20260112"] = "compact_20260112"
    trigger: InputTokensTrigger | None = None
    pause_after_compaction: bool = False
    instructions: str | None = None


class ClearThinkingEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["clear_thinking_20251015"] = "clear_thinking_20251015"
    keep: KeepThinkingTurns | Literal["all"] | None = None


class ClearToolUsesEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["clear_tool_uses_20250919"] = "clear_tool_uses_20250919"
    trigger: InputTokensTrigger | ToolUsesTrigger | None = None
    keep: KeepToolUses | None = None
    clear_at_least: ClearAtLeastInputTokens | None = None
    exclude_tools: list[str] | None = None
    clear_tool_inputs: bool | None = None


class ClaudeContextManagementConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edits: list[CompactEdit | ClearThinkingEdit | ClearToolUsesEdit] = Field(default_factory=list)


class ClaudeContextManagementPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True

    enable_compaction: bool = True
    compaction_trigger_tokens: int | None = None
    pause_after_compaction: bool = False
    compaction_instructions: str | None = None

    enable_clear_thinking: bool = True
    clear_thinking_keep: int | Literal["all"] = 2

    enable_clear_tool_uses: bool = True
    clear_tool_trigger_tokens: int | None = None
    clear_tool_keep: int = 5
    clear_tool_clear_at_least_tokens: int | None = None
    clear_tool_exclude: list[str] | None = None
    clear_tool_inputs: bool = False


class ClaudeContextManagementHelper:
    @staticmethod
    def _normalize_existing_betas_from_edits(context_management: ClaudeContextManagementConfig) -> list[str]:
        betas: list[str] = []
        for edit in context_management.edits:
            if isinstance(edit, CompactEdit):
                betas.append("compact-2026-01-12")
            if isinstance(edit, (ClearThinkingEdit, ClearToolUsesEdit)):
                betas.append("context-management-2025-06-27")
        return list(dict.fromkeys(betas))

    @staticmethod
    def _tools_present(chat_req: Any) -> bool:
        return bool(getattr(chat_req, "tools", None))

    @staticmethod
    def _thinking_enabled(chat_req: Any) -> bool:
        thinking = getattr(chat_req, "thinking", None)
        return isinstance(thinking, dict) and thinking.get("type") in {"enabled", "adaptive"}

    @staticmethod
    def _default_compaction_trigger(capabilities: Any) -> int:
        if capabilities.context_window and capabilities.context_window >= 1_000_000:
            return 150_000
        return 150_000

    @staticmethod
    def _default_tool_clear_trigger(capabilities: Any) -> int:
        if capabilities.context_window and capabilities.context_window >= 1_000_000:
            return 250_000
        return 100_000

    @staticmethod
    def _normalize_edit_order(
        edits: list[CompactEdit | ClearThinkingEdit | ClearToolUsesEdit],
    ) -> list[CompactEdit | ClearThinkingEdit | ClearToolUsesEdit]:
        thinking_edits = [edit for edit in edits if isinstance(edit, ClearThinkingEdit)]
        other_edits = [edit for edit in edits if not isinstance(edit, ClearThinkingEdit)]
        normalized = [*thinking_edits, *other_edits]
        if normalized != edits:
            logger.warning("Reordered context management edits so clear_thinking comes first")
        return normalized

    @classmethod
    def _validate_and_normalize_existing_context_management(
        cls,
        *,
        chat_req: Any,
        capabilities: Any,
    ) -> ClaudeContextManagementConfig | None:
        existing = getattr(chat_req, "context_management", None)
        if existing is None:
            return None

        edits = list(getattr(existing, "edits", []) or [])
        if not edits:
            return None

        normalized_edits: list[CompactEdit | ClearThinkingEdit | ClearToolUsesEdit] = []
        thinking_enabled = cls._thinking_enabled(chat_req)

        for edit in cls._normalize_edit_order(edits):
            if isinstance(edit, CompactEdit):
                if not capabilities.supports_compaction:
                    logger.warning(
                        "Dropping unsupported compaction edit for model={} provider={}",
                        getattr(chat_req, "model", None),
                        getattr(chat_req, "provider", None),
                    )
                    continue

                trigger = edit.trigger
                if trigger is None:
                    edit.trigger = InputTokensTrigger(
                        value=cls._default_compaction_trigger(capabilities)
                    )
                elif trigger.value < 50_000:
                    logger.warning(
                        "Raising compaction trigger from {} to 50000 to satisfy Anthropic minimum",
                        trigger.value,
                    )
                    trigger.value = 50_000

                normalized_edits.append(edit)
                continue

            if isinstance(edit, ClearThinkingEdit):
                if not capabilities.supports_context_management:
                    logger.warning("Dropping unsupported clear_thinking edit")
                    continue

                if not thinking_enabled:
                    logger.warning(
                        "Dropping clear_thinking edit because thinking is not enabled for this request"
                    )
                    continue

                if isinstance(edit.keep, KeepThinkingTurns) and edit.keep.value < 1:
                    logger.warning("Raising clear_thinking keep from {} to 1", edit.keep.value)
                    edit.keep.value = 1

                normalized_edits.append(edit)
                continue

            if isinstance(edit, ClearToolUsesEdit):
                if not capabilities.supports_context_management:
                    logger.warning("Dropping unsupported clear_tool_uses edit")
                    continue

                if isinstance(edit.trigger, InputTokensTrigger) and edit.trigger.value < 1:
                    logger.warning(
                        "Replacing invalid clear_tool_uses input_tokens trigger={} with default",
                        edit.trigger.value,
                    )
                    edit.trigger = InputTokensTrigger(
                        value=cls._default_tool_clear_trigger(capabilities)
                    )

                if isinstance(edit.trigger, ToolUsesTrigger) and edit.trigger.value < 1:
                    logger.warning("Raising clear_tool_uses tool_uses trigger from {} to 1", edit.trigger.value)
                    edit.trigger.value = 1

                if edit.keep is not None and edit.keep.value < 1:
                    logger.warning("Raising clear_tool_uses keep from {} to 1", edit.keep.value)
                    edit.keep.value = 1

                if edit.clear_at_least is not None and edit.clear_at_least.value < 1:
                    logger.warning(
                        "Dropping invalid clear_at_least={} because it must be positive",
                        edit.clear_at_least.value,
                    )
                    edit.clear_at_least = None

                if edit.exclude_tools:
                    edit.exclude_tools = list(dict.fromkeys(edit.exclude_tools))

                normalized_edits.append(edit)
                continue

        if not normalized_edits:
            return None

        return ClaudeContextManagementConfig(edits=normalized_edits)

    @classmethod
    def apply(
        cls,
        *,
        chat_req: Any,
        context_decision: Any,
    ) -> None:
        resolved = ClaudeModelRegistry.resolve(str(chat_req.model), chat_req.provider)
        capabilities = resolved.capabilities

        existing = cls._validate_and_normalize_existing_context_management(
            chat_req=chat_req,
            capabilities=capabilities,
        )
        if existing is not None and getattr(existing, "edits", None):
            chat_req.context_management = existing
            betas = cls._normalize_existing_betas_from_edits(existing)
            chat_req.betas = list(dict.fromkeys((chat_req.betas or []) + betas))
            logger.info(
                "Using caller-supplied context management edits={} betas={}",
                [edit.type for edit in existing.edits],
                betas,
            )
            return

        policy = getattr(chat_req, "context_management_policy", None)
        if policy is None:
            policy = ClaudeContextManagementPolicy()
            chat_req.context_management_policy = policy

        if not policy.enabled or not capabilities.supports_context_management:
            return

        edits: list[CompactEdit | ClearThinkingEdit | ClearToolUsesEdit] = []
        betas: list[str] = []

        if policy.enable_clear_thinking and cls._thinking_enabled(chat_req):
            if policy.clear_thinking_keep == "all":
                edits.append(ClearThinkingEdit(keep="all"))
            else:
                edits.append(
                    ClearThinkingEdit(
                        keep=KeepThinkingTurns(value=max(1, int(policy.clear_thinking_keep)))
                    )
                )
            betas.append("context-management-2025-06-27")

        if policy.enable_clear_tool_uses and cls._tools_present(chat_req):
            clear_tool_edit = ClearToolUsesEdit(
                trigger=InputTokensTrigger(
                    value=max(
                        1,
                        policy.clear_tool_trigger_tokens
                        or cls._default_tool_clear_trigger(capabilities),
                    )
                ),
                keep=KeepToolUses(value=max(1, int(policy.clear_tool_keep))),
                clear_at_least=(
                    ClearAtLeastInputTokens(value=policy.clear_tool_clear_at_least_tokens)
                    if policy.clear_tool_clear_at_least_tokens
                    else None
                ),
                exclude_tools=list(dict.fromkeys(policy.clear_tool_exclude or [])) or None,
                clear_tool_inputs=policy.clear_tool_inputs,
            )
            edits.append(clear_tool_edit)
            betas.append("context-management-2025-06-27")

        if policy.enable_compaction and capabilities.supports_compaction:
            compact_edit = CompactEdit(
                trigger=InputTokensTrigger(
                    value=max(
                        50_000,
                        policy.compaction_trigger_tokens
                        or cls._default_compaction_trigger(capabilities),
                    )
                ),
                pause_after_compaction=policy.pause_after_compaction,
                instructions=policy.compaction_instructions,
            )
            edits.append(compact_edit)
            betas.append("compact-2026-01-12")

        if not edits:
            return

        edits = cls._normalize_edit_order(edits)
        chat_req.context_management = ClaudeContextManagementConfig(edits=edits)
        chat_req.betas = list(dict.fromkeys((chat_req.betas or []) + betas))

        logger.info(
            "Applied context management edits={} betas={} should_try_compaction={} "
            "effective_input_tokens={} original_input_tokens={}",
            [edit.type for edit in edits],
            betas,
            getattr(context_decision, "should_try_compaction", False),
            getattr(context_decision, "estimated_input_tokens", None),
            getattr(context_decision, "original_input_tokens", None),
        )