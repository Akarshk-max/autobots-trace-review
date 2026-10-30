import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel, Field

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

FALLBACK_MODELS_PATH = Path(__file__).parent / "openrouter_models.json"
CATALOGUE_TTL_SECONDS = 1800
FETCH_TIMEOUT_SECONDS = 15
FAILED_FETCH_RETRY_SECONDS = 60


class ModelCapabilities(BaseModel):
    tool_calling: bool = False
    vision: bool = False
    structured_output: bool = False
    reasoning: bool = False
    file_input: bool = False
    video_input: bool = False
    audio_input: bool = False


class ModelInfo(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    context_length: int = 0
    modality: str = "unknown"
    input_modalities: list[str] = Field(default_factory=list)
    output_modalities: list[str] = Field(default_factory=list)
    capabilities: ModelCapabilities


def _capabilities_from_api(model: dict[str, Any]) -> ModelCapabilities:
    supported_parameters = model.get("supported_parameters") or []
    input_modalities = (model.get("architecture") or {}).get("input_modalities") or []
    return ModelCapabilities(
        tool_calling="tools" in supported_parameters,
        vision="image" in input_modalities,
        structured_output="structured_outputs" in supported_parameters
        or "response_format" in supported_parameters,
        reasoning="reasoning" in supported_parameters
        or "include_reasoning" in supported_parameters,
        file_input="file" in input_modalities,
        video_input="video" in input_modalities,
        audio_input="audio" in input_modalities,
    )


def _model_info_from_api(model: dict[str, Any]) -> ModelInfo | None:
    model_id = model.get("id")
    if not model_id:
        return None
    architecture = model.get("architecture") or {}
    return ModelInfo(
        id=model_id,
        name=model.get("name") or "",
        description=model.get("description") or "",
        context_length=model.get("context_length") or 0,
        modality=architecture.get("modality") or "unknown",
        input_modalities=architecture.get("input_modalities") or [],
        output_modalities=architecture.get("output_modalities") or [],
        capabilities=_capabilities_from_api(model),
    )


def _model_info_from_file(model_id: str, model: dict[str, Any]) -> ModelInfo | None:
    try:
        return ModelInfo(
            id=model.get("id") or model_id,
            name=model.get("name") or "",
            description=model.get("description") or "",
            context_length=model.get("context_length") or 0,
            modality=model.get("modality") or "unknown",
            input_modalities=model.get("input_modalities") or [],
            output_modalities=model.get("output_modalities") or [],
            capabilities=ModelCapabilities.model_validate(model.get("capabilities") or {}),
        )
    except Exception as e:
        logger.warning(f"Skipping malformed model '{model_id}' in fallback file: {e}")
        return None


class ModelCatalogue:

    def __init__(
        self,
        models_url: str = OPENROUTER_MODELS_URL,
        fallback_path: Path = FALLBACK_MODELS_PATH,
        ttl_seconds: int = CATALOGUE_TTL_SECONDS,
        timeout_seconds: int = FETCH_TIMEOUT_SECONDS,
    ) -> None:
        self.models_url = models_url
        self.fallback_path = fallback_path
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds

        self._models: dict[str, ModelInfo] = {}
        self._loaded_at: float | None = None
        self._last_failed_fetch_at: float | None = None
        self._loaded_from_openrouter = False
        self._seeded_from_fallback = False
        self._refresh_lock = asyncio.Lock()
        self._background_refresh: asyncio.Task | None = None

    def is_stale(self) -> bool:
        if self._loaded_at is None:
            return True
        return (time.monotonic() - self._loaded_at) > self.ttl_seconds

    def model_ids(self) -> list[str]:
        self.seed_from_fallback_file()
        return list(self._models)

    def models_with(self, capability: str) -> list[str]:
        self.seed_from_fallback_file()
        return [
            model_id
            for model_id, model in self._models.items()
            if getattr(model.capabilities, capability, False)
        ]

    def seed_from_fallback_file(self) -> None:
        if self._seeded_from_fallback or self._models:
            return
        self._seeded_from_fallback = True

        if not self.fallback_path.exists():
            logger.warning(f"OpenRouter fallback model file not found at {self.fallback_path}")
            return
        try:
            with open(self.fallback_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            logger.warning(f"Could not read OpenRouter fallback model file: {e}")
            return

        models: dict[str, ModelInfo] = {}
        for model_id, model in (payload.get("models") or {}).items():
            model_info = _model_info_from_file(model_id, model)
            if model_info is not None:
                models[model_info.id] = model_info

        self._models = models
        logger.info(
            f"Seeded OpenRouter catalogue with {len(models)} models "
            f"generated at {payload.get('generated_at')}"
        )

    async def refresh(self) -> bool:
        async with self._refresh_lock:
            if not self.is_stale() and self._loaded_from_openrouter:
                return True
            if self.is_in_failure_cooldown():
                return False
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.get(self.models_url)
                    response.raise_for_status()
                    payload = response.json()
            except Exception as e:
                self._last_failed_fetch_at = time.monotonic()
                logger.warning(f"OpenRouter model refresh failed, serving existing catalogue: {e}")
                return False

            models: dict[str, ModelInfo] = {}
            for model in payload.get("data") or []:
                model_info = _model_info_from_api(model)
                if model_info is not None:
                    models[model_info.id] = model_info

            if not models:
                self._last_failed_fetch_at = time.monotonic()
                logger.warning("OpenRouter returned no models, serving existing catalogue")
                return False

            self._models = models
            self._loaded_at = time.monotonic()
            self._loaded_from_openrouter = True
            self._last_failed_fetch_at = None
            logger.info(f"Refreshed OpenRouter catalogue with {len(models)} models")
            return True

    def refresh_in_background(self) -> None:
        if self.is_in_failure_cooldown():
            return
        if self._background_refresh is not None and not self._background_refresh.done():
            return
        try:
            self._background_refresh = asyncio.create_task(self.refresh())
        except RuntimeError:
            return

    def is_in_failure_cooldown(self) -> bool:
        if self._last_failed_fetch_at is None:
            return False
        return (time.monotonic() - self._last_failed_fetch_at) <= FAILED_FETCH_RETRY_SECONDS

    async def find(self, model_id: str) -> ModelInfo | None:
        self.seed_from_fallback_file()

        if self.is_stale():
            self.refresh_in_background()

        model = self._models.get(model_id)
        if model is not None:
            return model

        if not self._loaded_from_openrouter:
            await self.refresh()
            model = self._models.get(model_id)

        return model


_catalogue = ModelCatalogue()


async def get_model_capabilities(model_id: str) -> ModelInfo | None:
    return await _catalogue.find(model_id)


def get_all_model_ids() -> list[str]:
    return _catalogue.model_ids()


def get_models_with_tool_calling() -> list[str]:
    return _catalogue.models_with("tool_calling")


def get_models_with_vision() -> list[str]:
    return _catalogue.models_with("vision")
