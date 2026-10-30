from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict

from src.autobots.conn.claude.model_registry import ClaudeModelRegistry, ClaudeProvider


class ClaudeContextPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    provider: ClaudeProvider = ClaudeProvider.ANTHROPIC
    max_tokens: int
    messages: list[dict[str, Any]]
    system: str | list[dict[str, Any]] | None = None
    tools_count: int = 0
    exact_input_tokens: int | None = None
    original_input_tokens: int | None = None
    token_count_source: str | None = None


class ClaudeContextPolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estimated_input_tokens: int
    original_input_tokens: int | None = None
    max_context_window: int | None = None
    should_enable_prompt_caching: bool = False
    should_try_compaction: bool = False
    should_trim_old_messages: bool = False
    keep_last_messages: int = 12
    token_count_source: str = "heuristic"


class ClaudeContextPolicy:
    @staticmethod
    def _estimate_tokens_from_text(text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 4)

    @classmethod
    def _estimate_message_tokens(cls, message: dict[str, Any]) -> int:
        content = message.get("content")
        if isinstance(content, str):
            return cls._estimate_tokens_from_text(content)

        total = 0
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += cls._estimate_tokens_from_text(str(block.get("text", "")))
                    total += cls._estimate_tokens_from_text(str(block.get("title", "")))
                    total += cls._estimate_tokens_from_text(str(block.get("content", "")))
                    total += cls._estimate_tokens_from_text(str(block.get("thinking", "")))
        return total

    @classmethod
    def decide(cls, payload: ClaudeContextPolicyInput) -> ClaudeContextPolicyDecision:
        resolved = ClaudeModelRegistry.resolve(payload.model, payload.provider)
        max_window = resolved.capabilities.context_window

        if payload.exact_input_tokens is not None:
            estimated = payload.exact_input_tokens
            token_count_source = payload.token_count_source or "api"
        else:
            estimated = 0
            if isinstance(payload.system, str):
                estimated += cls._estimate_tokens_from_text(payload.system)
            elif isinstance(payload.system, list):
                for block in payload.system:
                    estimated += cls._estimate_tokens_from_text(str(block.get("text", "")))

            for message in payload.messages:
                estimated += cls._estimate_message_tokens(message)

            estimated += payload.tools_count * 250
            token_count_source = "heuristic"

        original_input_tokens = payload.original_input_tokens or estimated
        pressure_tokens = estimated + payload.max_tokens

        should_cache = resolved.capabilities.supports_prompt_caching and max(
            estimated,
            original_input_tokens,
        ) >= 12000
        should_compact = (
            resolved.capabilities.supports_compaction
            and max_window is not None
            and pressure_tokens >= int(max_window * 0.70)
        )
        should_trim = (
            max_window is not None
            and pressure_tokens >= int(max_window * 0.92)
        )

        decision = ClaudeContextPolicyDecision(
            estimated_input_tokens=estimated,
            original_input_tokens=original_input_tokens,
            max_context_window=max_window,
            should_enable_prompt_caching=should_cache,
            should_try_compaction=should_compact,
            should_trim_old_messages=should_trim,
            keep_last_messages=12 if should_trim else 24,
            token_count_source=token_count_source,
        )

        logger.info(
            "Claude context policy model={} effective_input_tokens={} original_input_tokens={} "
            "source={} cache={} compact={} trim={}",
            payload.model,
            decision.estimated_input_tokens,
            decision.original_input_tokens,
            decision.token_count_source,
            decision.should_enable_prompt_caching,
            decision.should_try_compaction,
            decision.should_trim_old_messages,
        )
        return decision