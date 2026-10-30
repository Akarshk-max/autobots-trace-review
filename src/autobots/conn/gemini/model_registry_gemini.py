from enum import Enum

from loguru import logger
from pydantic import BaseModel, ConfigDict


class GeminiProvider(str, Enum):
    GEMINI_API = "gemini_api"
    VERTEX = "vertex"


class GeminiModelLifecycle(str, Enum):
    STABLE = "stable"
    PREVIEW = "preview"
    EXPERIMENTAL = "experimental"
    DEPRECATED = "deprecated"


class GeminiThoughtSignatureMode(str, Enum):
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED_FOR_FUNCTION_CALLS = "required_for_function_calls"


class GeminiModelCapabilities(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    prefix: str
    family: str
    lifecycle: GeminiModelLifecycle = GeminiModelLifecycle.STABLE

    input_token_limit: int | None = None
    output_token_limit: int | None = None

    supports_function_calling: bool = False
    supports_parallel_function_calling: bool = False
    supports_compositional_function_calling: bool = False

    supports_code_execution: bool = False
    supports_file_search: bool = False
    supports_url_context: bool = False
    supports_search_grounding: bool = False
    supports_maps_grounding: bool = False

    supports_thinking: bool = False
    supports_structured_outputs: bool = False
    supports_streaming: bool = True
    supports_sdk_chat: bool = True

    supports_builtin_custom_tool_combination: bool = False
    supports_gemini_files_api: bool = True

    thought_signature_mode: GeminiThoughtSignatureMode = GeminiThoughtSignatureMode.NONE


class GeminiModelResolution(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    provider: GeminiProvider
    requested_model: str
    normalized_model: str
    capabilities: GeminiModelCapabilities


class GeminiModelRegistry:
    DEFAULT_MODEL = "gemini-3.5-flash"

    _MODELS: tuple[GeminiModelCapabilities, ...] = (

        GeminiModelCapabilities(
            prefix="gemini-3.7-flash",
            family="gemini-3.7",
            lifecycle=GeminiModelLifecycle.STABLE,
            input_token_limit=1_048_576,
            output_token_limit=65_536,
            supports_function_calling=True,
            supports_parallel_function_calling=True,
            supports_compositional_function_calling=True,
            supports_code_execution=True,
            supports_file_search=True,
            supports_url_context=True,
            supports_search_grounding=True,
            supports_maps_grounding=True,
            supports_thinking=True,
            supports_structured_outputs=True,
            supports_builtin_custom_tool_combination=True,
            thought_signature_mode=GeminiThoughtSignatureMode.REQUIRED_FOR_FUNCTION_CALLS,
        ),

        GeminiModelCapabilities(
            prefix="gemini-3.6-flash",
            family="gemini-3.6",
            lifecycle=GeminiModelLifecycle.STABLE,
            input_token_limit=1_048_576,
            output_token_limit=65_536,
            supports_function_calling=True,
            supports_parallel_function_calling=True,
            supports_compositional_function_calling=True,
            supports_code_execution=True,
            supports_file_search=True,
            supports_url_context=True,
            supports_search_grounding=True,
            supports_maps_grounding=True,
            supports_thinking=True,
            supports_structured_outputs=True,
            supports_builtin_custom_tool_combination=True,
            thought_signature_mode=GeminiThoughtSignatureMode.REQUIRED_FOR_FUNCTION_CALLS,
        ),

        GeminiModelCapabilities(
            prefix="gemini-3.5-flash",
            family="gemini-3.5",
            lifecycle=GeminiModelLifecycle.STABLE,
            input_token_limit=1_048_576,
            output_token_limit=65_536,
            supports_function_calling=True,
            supports_parallel_function_calling=True,
            supports_compositional_function_calling=True,
            supports_code_execution=True,
            supports_file_search=True,
            supports_url_context=True,
            supports_search_grounding=True,
            supports_maps_grounding=True,
            supports_thinking=True,
            supports_structured_outputs=True,
            supports_builtin_custom_tool_combination=True,
            thought_signature_mode=GeminiThoughtSignatureMode.REQUIRED_FOR_FUNCTION_CALLS,
        ),

        GeminiModelCapabilities(
            prefix="gemini-3.5-flash-lite",
            family="gemini-3.5",
            lifecycle=GeminiModelLifecycle.STABLE,
            input_token_limit=1_048_576,
            output_token_limit=65_536,
            supports_function_calling=True,
            supports_parallel_function_calling=True,
            supports_compositional_function_calling=True,
            supports_code_execution=True,
            supports_file_search=True,
            supports_url_context=True,
            supports_search_grounding=True,
            supports_maps_grounding=True,
            supports_thinking=True,
            supports_structured_outputs=True,
            supports_builtin_custom_tool_combination=True,
            thought_signature_mode=GeminiThoughtSignatureMode.REQUIRED_FOR_FUNCTION_CALLS,
        ),

        GeminiModelCapabilities(
            prefix="gemini-3.1-flash-lite",
            family="gemini-3.1",
            lifecycle=GeminiModelLifecycle.STABLE,
            input_token_limit=1_048_576,
            output_token_limit=65_536,
            supports_function_calling=True,
            supports_parallel_function_calling=True,
            supports_compositional_function_calling=True,
            supports_code_execution=True,
            supports_file_search=True,
            supports_url_context=True,
            supports_search_grounding=True,
            supports_maps_grounding=True,
            supports_thinking=True,
            supports_structured_outputs=True,
            supports_builtin_custom_tool_combination=True,
            thought_signature_mode=GeminiThoughtSignatureMode.REQUIRED_FOR_FUNCTION_CALLS,
        ),

        GeminiModelCapabilities(
            prefix="gemini-3.1-pro-preview",
            family="gemini-3.1",
            lifecycle=GeminiModelLifecycle.PREVIEW,
            input_token_limit=1_048_576,
            output_token_limit=65_536,
            supports_function_calling=True,
            supports_parallel_function_calling=True,
            supports_compositional_function_calling=True,
            supports_code_execution=True,
            supports_file_search=True,
            supports_url_context=True,
            supports_search_grounding=True,
            supports_maps_grounding=True,
            supports_thinking=True,
            supports_structured_outputs=True,
            supports_builtin_custom_tool_combination=True,
            thought_signature_mode=GeminiThoughtSignatureMode.REQUIRED_FOR_FUNCTION_CALLS,
        ),
        GeminiModelCapabilities(
            prefix="gemini-3.1-pro-preview-customtools",
            family="gemini-3.1",
            lifecycle=GeminiModelLifecycle.PREVIEW,
            input_token_limit=1_048_576,
            output_token_limit=65_536,
            supports_function_calling=True,
            supports_parallel_function_calling=True,
            supports_compositional_function_calling=True,
            supports_code_execution=True,
            supports_file_search=True,
            supports_url_context=True,
            supports_search_grounding=True,
            supports_maps_grounding=True,
            supports_thinking=True,
            supports_structured_outputs=True,
            supports_builtin_custom_tool_combination=True,
            thought_signature_mode=GeminiThoughtSignatureMode.REQUIRED_FOR_FUNCTION_CALLS,
        ),
        GeminiModelCapabilities(
            prefix="gemini-3-flash-preview",
            family="gemini-3",
            lifecycle=GeminiModelLifecycle.PREVIEW,
            input_token_limit=1_048_576,
            output_token_limit=65_536,
            supports_function_calling=True,
            supports_parallel_function_calling=True,
            supports_compositional_function_calling=True,
            supports_code_execution=True,
            supports_file_search=True,
            supports_url_context=True,
            supports_search_grounding=True,
            supports_maps_grounding=True,
            supports_thinking=True,
            supports_structured_outputs=True,
            supports_builtin_custom_tool_combination=True,
            thought_signature_mode=GeminiThoughtSignatureMode.REQUIRED_FOR_FUNCTION_CALLS,
        ),
        GeminiModelCapabilities(
            prefix="gemini-3.1-flash-lite-preview",
            family="gemini-3.1",
            lifecycle=GeminiModelLifecycle.PREVIEW,
            input_token_limit=1_048_576,
            output_token_limit=65_536,
            supports_function_calling=True,
            supports_parallel_function_calling=True,
            supports_compositional_function_calling=True,
            supports_code_execution=True,
            supports_file_search=True,
            supports_url_context=True,
            supports_search_grounding=True,
            supports_maps_grounding=True,
            supports_thinking=True,
            supports_structured_outputs=True,
            supports_builtin_custom_tool_combination=True,
            thought_signature_mode=GeminiThoughtSignatureMode.REQUIRED_FOR_FUNCTION_CALLS,
        ),

        # Legacy Gemini 3 aliases / retired preview IDs kept for safe resolution of older configs.
        # GeminiModelCapabilities(
        #     prefix="gemini-3-pro-preview",
        #     family="gemini-3",
        #     lifecycle=GeminiModelLifecycle.DEPRECATED,
        #     input_token_limit=1_048_576,
        #     output_token_limit=65_536,
        #     supports_function_calling=True,
        #     supports_parallel_function_calling=True,
        #     supports_compositional_function_calling=True,
        #     supports_code_execution=True,
        #     supports_file_search=True,
        #     supports_url_context=True,
        #     supports_search_grounding=True,
        #     supports_maps_grounding=True,
        #     supports_thinking=True,
        #     supports_structured_outputs=True,
        #     supports_builtin_custom_tool_combination=True,
        #     thought_signature_mode=GeminiThoughtSignatureMode.REQUIRED_FOR_FUNCTION_CALLS,
        # ),
        # GeminiModelCapabilities(
        #     prefix="gemini-3-pro",
        #     family="gemini-3",
        #     lifecycle=GeminiModelLifecycle.DEPRECATED,
        #     input_token_limit=1_048_576,
        #     output_token_limit=65_536,
        #     supports_function_calling=True,
        #     supports_parallel_function_calling=True,
        #     supports_compositional_function_calling=True,
        #     supports_code_execution=True,
        #     supports_file_search=True,
        #     supports_url_context=True,
        #     supports_search_grounding=True,
        #     supports_maps_grounding=True,
        #     supports_thinking=True,
        #     supports_structured_outputs=True,
        #     supports_builtin_custom_tool_combination=True,
        #     thought_signature_mode=GeminiThoughtSignatureMode.REQUIRED_FOR_FUNCTION_CALLS,
        # ),
        # GeminiModelCapabilities(
        #     prefix="gemini-3-flash",
        #     family="gemini-3",
        #     lifecycle=GeminiModelLifecycle.DEPRECATED,
        #     input_token_limit=1_048_576,
        #     output_token_limit=65_536,
        #     supports_function_calling=True,
        #     supports_parallel_function_calling=True,
        #     supports_compositional_function_calling=True,
        #     supports_code_execution=True,
        #     supports_file_search=True,
        #     supports_url_context=True,
        #     supports_search_grounding=True,
        #     supports_maps_grounding=True,
        #     supports_thinking=True,
        #     supports_structured_outputs=True,
        #     supports_builtin_custom_tool_combination=True,
        #     thought_signature_mode=GeminiThoughtSignatureMode.REQUIRED_FOR_FUNCTION_CALLS,
        # ),
        GeminiModelCapabilities(
            prefix="gemini-2.5-pro",
            family="gemini-2.5",
            lifecycle=GeminiModelLifecycle.STABLE,
            input_token_limit=1_048_576,
            output_token_limit=65_536,
            supports_function_calling=True,
            supports_parallel_function_calling=True,
            supports_compositional_function_calling=True,
            supports_code_execution=True,
            supports_file_search=True,
            supports_url_context=True,
            supports_search_grounding=True,
            supports_maps_grounding=True,
            supports_thinking=True,
            supports_structured_outputs=True,
            thought_signature_mode=GeminiThoughtSignatureMode.OPTIONAL,
        ),
        GeminiModelCapabilities(
            prefix="gemini-2.5-flash",
            family="gemini-2.5",
            lifecycle=GeminiModelLifecycle.STABLE,
            input_token_limit=1_048_576,
            output_token_limit=65_536,
            supports_function_calling=True,
            supports_parallel_function_calling=True,
            supports_compositional_function_calling=True,
            supports_code_execution=True,
            supports_file_search=True,
            supports_url_context=True,
            supports_search_grounding=True,
            supports_maps_grounding=True,
            supports_thinking=True,
            supports_structured_outputs=True,
            thought_signature_mode=GeminiThoughtSignatureMode.OPTIONAL,
        ),
        GeminiModelCapabilities(
            prefix="gemini-2.5-flash-lite",
            family="gemini-2.5",
            lifecycle=GeminiModelLifecycle.STABLE,
            input_token_limit=1_048_576,
            output_token_limit=65_536,
            supports_function_calling=True,
            supports_parallel_function_calling=True,
            supports_compositional_function_calling=True,
            supports_code_execution=True,
            supports_file_search=True,
            supports_url_context=True,
            supports_search_grounding=True,
            supports_maps_grounding=True,
            supports_thinking=True,
            supports_structured_outputs=True,
            thought_signature_mode=GeminiThoughtSignatureMode.OPTIONAL,
        ),
        GeminiModelCapabilities(
            prefix="gemini-2.0-flash-lite",
            family="gemini-2.0",
            lifecycle=GeminiModelLifecycle.STABLE,
            input_token_limit=1_048_576,
            output_token_limit=8_192,
            supports_code_execution=True,
            supports_search_grounding=True,
            supports_maps_grounding=False,
            supports_thinking=False,
            supports_structured_outputs=False,
            thought_signature_mode=GeminiThoughtSignatureMode.NONE,
        ),
        GeminiModelCapabilities(
            prefix="gemini-2.0-flash",
            family="gemini-2.0",
            lifecycle=GeminiModelLifecycle.STABLE,
            input_token_limit=1_048_576,
            output_token_limit=8_192,
            supports_function_calling=True,
            supports_parallel_function_calling=True,
            supports_compositional_function_calling=True,
            supports_code_execution=True,
            supports_search_grounding=True,
            supports_maps_grounding=True,
            supports_thinking=False,
            thought_signature_mode=GeminiThoughtSignatureMode.NONE,
        ),
    )

    _FALLBACK = GeminiModelCapabilities(
        prefix="gemini-",
        family="unknown",
        lifecycle=GeminiModelLifecycle.PREVIEW,
        input_token_limit=1_048_576,
        output_token_limit=65_536,
        supports_function_calling=True,
        supports_parallel_function_calling=True,
        supports_compositional_function_calling=True,
        supports_code_execution=True,
        supports_thinking=True,
        supports_structured_outputs=True,
        thought_signature_mode=GeminiThoughtSignatureMode.OPTIONAL,
    )

    @classmethod
    def default_model(cls) -> str:
        return cls.DEFAULT_MODEL

    @classmethod
    def resolve(
        cls,
        model: str | None,
        provider: GeminiProvider = GeminiProvider.GEMINI_API,
    ) -> GeminiModelResolution:
        requested = str(model or cls.DEFAULT_MODEL)
        matched = max(
            (cap for cap in cls._MODELS if requested.startswith(cap.prefix)),
            key=lambda cap: len(cap.prefix),
            default=cls._FALLBACK,
        )

        if provider == GeminiProvider.VERTEX:
            matched = matched.model_copy(
                update={
                    "supports_gemini_files_api": False,
                    "supports_sdk_chat": False,
                }
            )

        logger.debug(
            "Gemini model resolved provider={} model={} family={} lifecycle={} input_limit={} "
            "output_limit={} function_calling={} code_execution={} builtin_custom_combination={} "
            "signature_mode={}",
            provider.value,
            requested,
            matched.family,
            matched.lifecycle.value,
            matched.input_token_limit,
            matched.output_token_limit,
            matched.supports_function_calling,
            matched.supports_code_execution,
            matched.supports_builtin_custom_tool_combination,
            matched.thought_signature_mode.value,
        )

        return GeminiModelResolution(
            provider=provider,
            requested_model=requested,
            normalized_model=requested,
            capabilities=matched,
        )

    @classmethod
    def supports_main_action_custom_tools(
        cls,
        model: str | None,
        provider: GeminiProvider = GeminiProvider.GEMINI_API,
    ) -> bool:
        return cls.resolve(model=model, provider=provider).capabilities.supports_function_calling