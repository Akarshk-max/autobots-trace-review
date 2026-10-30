from pydantic import BaseModel


class PerplexityModelCapabilities(BaseModel):
    supports_streaming: bool = True
    supports_stream_mode_concise: bool = True
    supports_search_grounding: bool = True
    supports_citations: bool = True
    supports_search_results: bool = True
    supports_related_questions: bool = True
    supports_return_images: bool = True
    supports_return_videos: bool = True
    supports_image_inputs: bool = True
    supports_file_inputs: bool = True
    supports_structured_output: bool = True
    supports_pro_search: bool = False
    supports_auto_search_classifier: bool = False
    supports_reasoning: bool = False
    supports_async_api: bool = True


class PerplexityResolvedModel(BaseModel):
    model: str
    capabilities: PerplexityModelCapabilities


class PerplexityModelRegistry:
    _CAPS = {
        "sonar": PerplexityModelCapabilities(
            supports_pro_search=False,
            supports_auto_search_classifier=False,
            supports_reasoning=False,
        ),
        "sonar-pro": PerplexityModelCapabilities(
            supports_pro_search=True,
            supports_auto_search_classifier=True,
            supports_reasoning=False,
        ),
        "sonar-reasoning-pro": PerplexityModelCapabilities(
            supports_pro_search=False,
            supports_auto_search_classifier=False,
            supports_reasoning=True,
        ),
        "sonar-deep-research": PerplexityModelCapabilities(
            supports_pro_search=False,
            supports_auto_search_classifier=False,
            supports_reasoning=True,
            supports_return_images=False,
        ),
    }

    @classmethod
    def resolve(cls, model: str) -> PerplexityResolvedModel:
        normalized = (model or "sonar-pro").strip()
        capabilities = cls._CAPS.get(normalized)
        if capabilities is None:
            capabilities = PerplexityModelCapabilities()
        return PerplexityResolvedModel(model=normalized, capabilities=capabilities)