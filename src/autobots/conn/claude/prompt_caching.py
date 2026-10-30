from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict


class ClaudeCacheControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["ephemeral"] = "ephemeral"
    ttl: Literal["1h"] | None = None


class ClaudePromptCachingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    automatic: bool = True
    ttl: Literal["5m", "1h"] = "5m"
    cache_system_prompt: bool = True
    cache_compaction_blocks: bool = True
    min_estimated_tokens: int = 12000


class ClaudePromptCachingHelper:
    @staticmethod
    def _cache_control_from_config(config: ClaudePromptCachingConfig) -> ClaudeCacheControl:
        if config.ttl == "1h":
            return ClaudeCacheControl(type="ephemeral", ttl="1h")
        return ClaudeCacheControl(type="ephemeral")

    @staticmethod
    def _normalize_block(block: Any) -> dict[str, Any]:
        if isinstance(block, dict):
            return dict(block)
        if hasattr(block, "model_dump"):
            return block.model_dump()
        return {"type": "text", "text": str(block)}

    @classmethod
    def _count_explicit_breakpoints(cls, tools: list[Any] | None, system: Any, messages: list[dict[str, Any]]) -> int:
        count = 0

        for tool in tools or []:
            tool_dict = cls._normalize_block(tool)
            if tool_dict.get("cache_control") is not None:
                count += 1

        if isinstance(system, list):
            for block in system:
                block_dict = cls._normalize_block(block)
                if block_dict.get("cache_control") is not None:
                    count += 1

        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    block_dict = cls._normalize_block(block)
                    if block_dict.get("cache_control") is not None:
                        count += 1

        return count

    @classmethod
    def _ensure_system_cache_breakpoint(
        cls,
        system: str | list[dict[str, Any]] | None,
        cache_control: ClaudeCacheControl,
    ) -> str | list[dict[str, Any]] | None:
        cache_dict = cache_control.model_dump(exclude_none=True)

        if system is None:
            return system

        if isinstance(system, str):
            return [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": cache_dict,
                }
            ]

        normalized = [cls._normalize_block(block) for block in system]
        if not normalized:
            return normalized

        for idx in range(len(normalized) - 1, -1, -1):
            if normalized[idx].get("type") == "text":
                if normalized[idx].get("cache_control") is None:
                    normalized[idx]["cache_control"] = cache_dict
                return normalized

        if normalized[-1].get("cache_control") is None:
            normalized[-1]["cache_control"] = cache_dict

        return normalized

    @classmethod
    def _ensure_compaction_block_caching(
        cls,
        messages: list[dict[str, Any]],
        cache_control: ClaudeCacheControl,
    ) -> list[dict[str, Any]]:
        cache_dict = cache_control.model_dump(exclude_none=True)
        updated_messages: list[dict[str, Any]] = []

        for message in messages:
            new_message = dict(message)
            content = new_message.get("content")

            if not isinstance(content, list):
                updated_messages.append(new_message)
                continue

            new_content: list[dict[str, Any]] = []
            for block in content:
                block_dict = cls._normalize_block(block)
                if block_dict.get("type") == "compaction" and block_dict.get("cache_control") is None:
                    block_dict["cache_control"] = cache_dict
                new_content.append(block_dict)

            new_message["content"] = new_content
            updated_messages.append(new_message)

        return updated_messages

    @classmethod
    def apply(
        cls,
        *,
        chat_req: Any,
        context_decision: Any,
        capabilities: Any,
    ) -> None:
        if not capabilities.supports_prompt_caching:
            return

        config = getattr(chat_req, "prompt_caching", None)
        if config is None:
            if not context_decision.should_enable_prompt_caching:
                return
            default_ttl = "1h" if isinstance(getattr(chat_req, "thinking", None), dict) else "5m"
            config = ClaudePromptCachingConfig(ttl=default_ttl)
            chat_req.prompt_caching = config

        if not config.enabled:
            return

        if context_decision.estimated_input_tokens < config.min_estimated_tokens:
            return

        cache_control = cls._cache_control_from_config(config)
        explicit_breakpoints = cls._count_explicit_breakpoints(
            tools=getattr(chat_req, "tools", None),
            system=getattr(chat_req, "system", None),
            messages=getattr(chat_req, "messages", []),
        )

        auto_slot_needed = 1 if config.automatic else 0
        available_explicit_slots = max(0, 4 - auto_slot_needed)

        if config.cache_system_prompt and explicit_breakpoints < available_explicit_slots:
            chat_req.system = cls._ensure_system_cache_breakpoint(chat_req.system, cache_control)
            explicit_breakpoints += 1

        if config.cache_compaction_blocks and explicit_breakpoints < 4:
            chat_req.messages = cls._ensure_compaction_block_caching(chat_req.messages, cache_control)

        if config.automatic and getattr(chat_req, "cache_control", None) is None:
            chat_req.cache_control = cache_control

        logger.info(
            "Applied prompt caching ttl={} automatic={} cache_system_prompt={} "
            "cache_compaction_blocks={} effective_input_tokens={}",
            config.ttl,
            config.automatic,
            config.cache_system_prompt,
            config.cache_compaction_blocks,
            context_decision.estimated_input_tokens,
        )