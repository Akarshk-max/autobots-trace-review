from typing import Any, Dict

from src.autobots.token_usage.token_usage_doc_model import LlmCallUsage

GROK_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "reasoning_tokens",
    "prompt_text_tokens",
    "cached_prompt_text_tokens",
    "prompt_image_tokens",
    "num_sources_used",
)


def _as_dict(usage: Any) -> Dict[str, Any]:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return dict(usage)
    if hasattr(usage, "model_dump"):
        try:
            return usage.model_dump(exclude_none=True)
        except Exception:
            return {}
    return {}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _openai_chat_shape(
    raw: Dict[str, Any], model: str, loop: int, cost: float | None = None
) -> LlmCallUsage:
    prompt_tokens = _int(raw.get("prompt_tokens"))
    completion_tokens = _int(raw.get("completion_tokens"))
    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    cached_input_tokens = _int(prompt_details.get("cached_tokens"))
    cache_write_tokens = _int(prompt_details.get("cache_write_tokens"))

    return LlmCallUsage(
        loop=loop,
        model=model,
        input_tokens=max(prompt_tokens - cached_input_tokens - cache_write_tokens, 0),
        output_tokens=completion_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=_int(completion_details.get("reasoning_tokens")),
        total_tokens=_int(raw.get("total_tokens")) or (prompt_tokens + completion_tokens),
        cost=cost,
        raw=raw,
    )


def normalize_claude(usage: Any, model: str, loop: int) -> LlmCallUsage:
    raw = _as_dict(usage)
    input_tokens = _int(raw.get("input_tokens"))
    output_tokens = _int(raw.get("output_tokens"))
    cached_input_tokens = _int(raw.get("cache_read_input_tokens"))
    cache_write_tokens = _int(raw.get("cache_creation_input_tokens"))

    return LlmCallUsage(
        loop=loop,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_tokens=cache_write_tokens,
        total_tokens=input_tokens + cached_input_tokens + cache_write_tokens + output_tokens,
        raw=raw,
    )


def normalize_openai_chat(usage: Any, model: str, loop: int) -> LlmCallUsage:
    return _openai_chat_shape(_as_dict(usage), model, loop)


def normalize_openrouter(usage: Any, model: str, loop: int) -> LlmCallUsage:
    raw = _as_dict(usage)
    return _openai_chat_shape(raw, model, loop, cost=_float(raw.get("cost")))


def normalize_openai_responses(usage: Any, model: str, loop: int) -> LlmCallUsage:
    raw = _as_dict(usage)
    input_tokens = _int(raw.get("input_tokens"))
    output_tokens = _int(raw.get("output_tokens"))
    input_details = raw.get("input_tokens_details") or {}
    output_details = raw.get("output_tokens_details") or {}
    cached_input_tokens = _int(input_details.get("cached_tokens"))
    cache_write_tokens = _int(input_details.get("cache_write_tokens"))

    return LlmCallUsage(
        loop=loop,
        model=model,
        input_tokens=max(input_tokens - cached_input_tokens - cache_write_tokens, 0),
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=_int(output_details.get("reasoning_tokens")),
        total_tokens=_int(raw.get("total_tokens")) or (input_tokens + output_tokens),
        raw=raw,
    )


def normalize_gemini(usage: Any, model: str, loop: int) -> LlmCallUsage:
    raw = _as_dict(usage)
    prompt_tokens = _int(raw.get("prompt_token_count"))
    cached_input_tokens = _int(raw.get("cached_content_token_count"))
    candidates_tokens = _int(raw.get("candidates_token_count"))
    thoughts_tokens = _int(raw.get("thoughts_token_count"))
    tool_use_tokens = _int(raw.get("tool_use_prompt_token_count"))
    output_tokens = candidates_tokens + thoughts_tokens

    return LlmCallUsage(
        loop=loop,
        model=model,
        input_tokens=max(prompt_tokens - cached_input_tokens, 0),
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        reasoning_tokens=thoughts_tokens,
        total_tokens=_int(raw.get("total_token_count"))
        or (prompt_tokens + tool_use_tokens + output_tokens),
        raw=raw,
    )


def normalize_deepseek(usage: Any, model: str, loop: int) -> LlmCallUsage:
    raw = _as_dict(usage)
    prompt_tokens = _int(raw.get("prompt_tokens"))
    completion_tokens = _int(raw.get("completion_tokens"))
    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    cached_input_tokens = _int(
        raw.get("prompt_cache_hit_tokens") or prompt_details.get("cached_tokens")
    )

    return LlmCallUsage(
        loop=loop,
        model=model,
        input_tokens=max(prompt_tokens - cached_input_tokens, 0),
        output_tokens=completion_tokens,
        cached_input_tokens=cached_input_tokens,
        reasoning_tokens=_int(completion_details.get("reasoning_tokens")),
        total_tokens=_int(raw.get("total_tokens")) or (prompt_tokens + completion_tokens),
        raw=raw,
    )


def normalize_perplexity(usage: Any, model: str, loop: int) -> LlmCallUsage:
    raw = _as_dict(usage)
    prompt_tokens = _int(raw.get("prompt_tokens"))
    completion_tokens = _int(raw.get("completion_tokens"))
    cost = raw.get("cost") or {}

    return LlmCallUsage(
        loop=loop,
        model=model,
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        reasoning_tokens=_int(raw.get("reasoning_tokens")),
        total_tokens=_int(raw.get("total_tokens")) or (prompt_tokens + completion_tokens),
        cost=_float(cost.get("total_cost")) if isinstance(cost, dict) else None,
        raw=raw,
    )


def normalize_grok(usage: Any, model: str, loop: int) -> LlmCallUsage:
    raw = _as_dict(usage)
    if not raw and usage is not None:
        raw = {
            field: getattr(usage, field)
            for field in GROK_USAGE_FIELDS
            if getattr(usage, field, None) is not None
        }

    prompt_tokens = _int(raw.get("prompt_tokens"))
    completion_tokens = _int(raw.get("completion_tokens"))
    prompt_details = raw.get("prompt_tokens_details") or {}
    cached_input_tokens = _int(
        raw.get("cached_prompt_text_tokens") or prompt_details.get("cached_tokens")
    )

    return LlmCallUsage(
        loop=loop,
        model=model,
        input_tokens=max(prompt_tokens - cached_input_tokens, 0),
        output_tokens=completion_tokens,
        cached_input_tokens=cached_input_tokens,
        reasoning_tokens=_int(raw.get("reasoning_tokens")),
        total_tokens=_int(raw.get("total_tokens")) or (prompt_tokens + completion_tokens),
        raw=raw,
    )
