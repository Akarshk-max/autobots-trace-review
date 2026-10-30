from enum import Enum

from loguru import logger
from pydantic import BaseModel, ConfigDict


class ClaudeProvider(str, Enum):
    ANTHROPIC = "anthropic"
    VERTEX = "vertex"
    
class ClaudeModelCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prefix: str
    family: str
    max_output_tokens: int | None = None
    context_window: int | None = None

    supports_thinking: bool = False
    supports_adaptive_thinking: bool = False
    supports_effort: bool = False  # add this

    supports_code_execution: bool = False
    supports_web_search: bool = False
    supports_web_fetch: bool = False
    supports_memory_tool: bool = False
    supports_files_api: bool = False
    supports_prompt_caching: bool = False
    supports_context_management: bool = False
    supports_compaction: bool = False
    rejects_assistant_prefill: bool = False
    # 4.7 or higher oder model configuration
    rejects_sampling_params: bool = False
    # only for claude fable 5 ( always on adaptive thinking )
    always_on_adaptive_thinking: bool = False
    rejects_forced_tool_choice: bool = False

class ClaudeModelResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ClaudeProvider
    requested_model: str
    normalized_model: str
    capabilities: ClaudeModelCapabilities


class ClaudeModelRegistry:
    _MODELS: tuple[ClaudeModelCapabilities, ...] = (
        
	ClaudeModelCapabilities(
		prefix="claude-fable-5-1",
		family="claude-fable-5",
		max_output_tokens=128000,
		context_window=1_000_000,
		supports_thinking=False,
		supports_adaptive_thinking=True,
		supports_effort=True,
		always_on_adaptive_thinking=True,
		rejects_forced_tool_choice=True,
		rejects_sampling_params=True,
		supports_code_execution=True,
		supports_web_search=True,
		supports_web_fetch=True,
		supports_memory_tool=True,
		supports_files_api=True,
		supports_prompt_caching=True,
		supports_context_management=True,
		supports_compaction=True,
		rejects_assistant_prefill=True,
	),

	ClaudeModelCapabilities(
		prefix="claude-fable-5",
		family="claude-fable-5",
		max_output_tokens=128000,
		context_window=1_000_000,
		supports_thinking=False,
		supports_adaptive_thinking=True,
		supports_effort=True,
		always_on_adaptive_thinking=True,
		rejects_sampling_params=True,
		supports_code_execution=True,
		supports_web_search=True,
		supports_web_fetch=True,
		supports_memory_tool=True,
		supports_files_api=True,
		supports_prompt_caching=True,
		supports_context_management=True,
		supports_compaction=True,
		rejects_assistant_prefill=True,
	),

	ClaudeModelCapabilities(
            prefix="claude-opus-5",
            family="claude-5",
            max_output_tokens=128000,
            context_window=1_000_000,
            supports_thinking=False,
            supports_adaptive_thinking=True,
            supports_effort=True,
            supports_code_execution=True,
            supports_web_search=True,
            supports_web_fetch=True,
            supports_memory_tool=True,
            supports_files_api=True,
            supports_prompt_caching=True,
            supports_context_management=True,
            supports_compaction=True,
            rejects_assistant_prefill=True,
            rejects_sampling_params=True,
        ),

	ClaudeModelCapabilities(
            prefix="claude-opus-4-8",
            family="claude-4.8",
            max_output_tokens=128000,
            context_window=1_000_000,
            supports_thinking=False,
            supports_adaptive_thinking=True,
            supports_effort=True,
            supports_code_execution=True,
            supports_web_search=True,
            supports_web_fetch=True,
            supports_memory_tool=True,
            supports_files_api=True,
            supports_prompt_caching=True,
            supports_context_management=True,
            supports_compaction=True,
            rejects_assistant_prefill=True,
            rejects_sampling_params=True,
        ),
        
	ClaudeModelCapabilities(
		prefix="claude-opus-4-7",
		family="claude-4.7",
		max_output_tokens=128000,
		context_window=1_000_000,
		supports_thinking=False,
		supports_adaptive_thinking=True,
		supports_effort=True,
		supports_code_execution=True,
		supports_web_search=True,
		supports_web_fetch=True,
		supports_memory_tool=True,
		supports_files_api=True,
		supports_prompt_caching=True,
		supports_context_management=True,
		supports_compaction=True,
		rejects_assistant_prefill=True,
        rejects_sampling_params=True,
	),
	ClaudeModelCapabilities(
		prefix="claude-opus-4-6",
		family="claude-4.6",
		max_output_tokens=128000,
		context_window=1_000_000,
		supports_thinking=True,
		supports_adaptive_thinking=True,
		supports_effort=True,
		supports_code_execution=True,
		supports_web_search=True,
		supports_web_fetch=True,
		supports_memory_tool=True,
		supports_files_api=True,
		supports_prompt_caching=True,
		supports_context_management=True,
		supports_compaction=True,
		rejects_assistant_prefill=True,
	),
	ClaudeModelCapabilities(
		prefix="claude-sonnet-4-6",
		family="claude-4.6",
		max_output_tokens=64000,
		context_window=1_000_000,
		supports_thinking=True,
		supports_adaptive_thinking=True,
		supports_effort=True,
		supports_code_execution=True,
		supports_web_search=True,
		supports_web_fetch=True,
		supports_memory_tool=True,
		supports_files_api=True,
		supports_prompt_caching=True,
		supports_context_management=True,
		supports_compaction=True,
		rejects_assistant_prefill=True,
	),
	ClaudeModelCapabilities(
		prefix="claude-sonnet-5",
		family="claude-5",
		max_output_tokens=128000,
		context_window=1_000_000,
		supports_thinking=False,
		supports_adaptive_thinking=True,
		supports_effort=True,
		supports_code_execution=True,
		supports_web_search=True,
		supports_web_fetch=True,
		supports_memory_tool=True,
		supports_files_api=True,
		supports_prompt_caching=True,
		supports_context_management=True,
		supports_compaction=True,
		rejects_assistant_prefill=True,
		rejects_sampling_params=True,
	),

	ClaudeModelCapabilities(
		prefix="claude-haiku-4-5",
		family="claude-4.5",
		max_output_tokens=64000,
		context_window=200000,
		supports_thinking=True,
		supports_adaptive_thinking=False,
		supports_effort=False,
		supports_code_execution=True,
		supports_web_search=True,
		supports_web_fetch=True,
		supports_memory_tool=True,
		supports_files_api=True,
		supports_prompt_caching=True,
		supports_context_management=True,
		supports_compaction=False,
		rejects_assistant_prefill=True,
	),
	ClaudeModelCapabilities(
		prefix="claude-sonnet-4-5",
		family="claude-4.5",
		max_output_tokens=64000,
		context_window=200000,
		supports_thinking=True,
		supports_adaptive_thinking=False,
		supports_effort=False,
		supports_code_execution=True,
		supports_web_search=True,
		supports_web_fetch=True,
		supports_memory_tool=True,
		supports_files_api=True,
		supports_prompt_caching=True,
		supports_context_management=True,
		supports_compaction=False,
		rejects_assistant_prefill=True,
	),
	ClaudeModelCapabilities(
		prefix="claude-opus-4-5",
		family="claude-4.5",
		max_output_tokens=64000,
		context_window=200000,
		supports_thinking=True,
		supports_adaptive_thinking=False,
		supports_effort=True,
		supports_code_execution=True,
		supports_web_search=True,
		supports_web_fetch=True,
		supports_memory_tool=True,
		supports_files_api=True,
		supports_prompt_caching=True,
		supports_context_management=True,
		supports_compaction=False,
		rejects_assistant_prefill=True,
	),

    )
    
    _FALLBACK = ClaudeModelCapabilities(
		prefix="claude-",
		family="unknown",
		max_output_tokens=4096,
		context_window=200000,
		supports_files_api=True,
		supports_prompt_caching=True,
		supports_effort=False,
	)

    @classmethod
    def resolve(
        cls,
        model: str,
        provider: ClaudeProvider = ClaudeProvider.ANTHROPIC,
    ) -> ClaudeModelResolution:
        model = str(model)
        matched = max(
            (cap for cap in cls._MODELS if model.startswith(cap.prefix)),
            key=lambda cap: len(cap.prefix),
            default=cls._FALLBACK,
        )

        if provider == ClaudeProvider.VERTEX:
            matched = matched.model_copy(
                update={
                    "supports_files_api": False,
                    "supports_code_execution": False,
                    "supports_memory_tool": False,
                    "supports_context_management": False,
                    "supports_compaction": False,
                }
            )

        logger.debug(
            "Claude model resolved provider={} model={} family={} context_window={} files_api={} "
            "code_execution={} prompt_caching={} context_management={} compaction={}",
            provider.value,
            model,
            matched.family,
            matched.context_window,
            matched.supports_files_api,
            matched.supports_code_execution,
            matched.supports_prompt_caching,
            matched.supports_context_management,
            matched.supports_compaction,
        )

        return ClaudeModelResolution(
            provider=provider,
            requested_model=model,
            normalized_model=model,
            capabilities=matched,
        )