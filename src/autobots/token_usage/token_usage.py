from typing import Dict, List

from src.autobots.token_usage.token_usage_doc_model import (
    LlmCallUsage,
    LlmSurface,
    TokenCounts,
)


class TokenUsageCollector:
    """Accumulates every LLM API call made during one action run attempt."""

    def __init__(self) -> None:
        self._calls_by_surface: Dict[LlmSurface, List[LlmCallUsage]] = {}

    def record(self, surface: LlmSurface, call: LlmCallUsage) -> None:
        self._calls_by_surface.setdefault(surface, []).append(call)

    def is_empty(self) -> bool:
        return not any(self._calls_by_surface.values())

    def calls_by_surface(self) -> Dict[LlmSurface, List[LlmCallUsage]]:
        return dict(self._calls_by_surface)

    @staticmethod
    def totals(calls: List[LlmCallUsage]) -> TokenCounts:

        priced = all(call.cost is not None for call in calls)
        return TokenCounts(
            input_tokens=sum(call.input_tokens for call in calls),
            output_tokens=sum(call.output_tokens for call in calls),
            cached_input_tokens=sum(call.cached_input_tokens for call in calls),
            cache_write_tokens=sum(call.cache_write_tokens for call in calls),
            reasoning_tokens=sum(call.reasoning_tokens for call in calls),
            total_tokens=sum(call.total_tokens for call in calls),
            cost=sum(call.cost or 0.0 for call in calls) if priced else None,
        )
