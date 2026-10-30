from typing import Any, Dict, Tuple, Type

from pydantic import BaseModel, TypeAdapter, ValidationError, create_model

from src.autobots.browser_task.intent.models import ScrapeIntentRequest


def _make_strict_model(base: Type[BaseModel]) -> Type[BaseModel]:
    """
    Create a strict twin of the model with extra=forbid.
    This helps us enforce strict_mode=True without duplicating all fields manually.
    """
    # Pydantic v2: create_model can inherit from base
    Strict = create_model(  # type: ignore[misc]
        f"{base.__name__}Strict",
        __base__=base,
    )
    Strict.model_config = {**getattr(base, "model_config", {}), "extra": "forbid"}
    return Strict


_ScrapeIntentStrict = _make_strict_model(ScrapeIntentRequest)


def parse_scrape_intent(payload: Dict[str, Any]) -> Tuple[ScrapeIntentRequest, Dict[str, Any]]:
    """
    Parses an LLM-facing intent payload into ScrapeIntentRequest.

    Returns:
      (intent_model, debug_info)

    debug_info contains:
      - strict_mode_used
      - validation_mode ("strict"/"lenient")
    """
    strict_mode = bool(payload.get("strict_mode", False))

    try:
        if strict_mode:
            # reject unknown fields
            intent = TypeAdapter(_ScrapeIntentStrict).validate_python(payload)  # type: ignore[arg-type]
            # cast back to the base model (same fields)
            intent_base = ScrapeIntentRequest.model_validate(intent.model_dump())
            return intent_base, {"strict_mode_used": True, "validation_mode": "strict"}

        # lenient: ignore unknown fields (base model config extra=ignore)
        intent = ScrapeIntentRequest.model_validate(payload)
        return intent, {"strict_mode_used": False, "validation_mode": "lenient"}

    except ValidationError as e:
        # Let caller decide how to report this; we keep it structured.
        raise e
