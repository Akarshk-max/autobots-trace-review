import json
from datetime import datetime, timezone
from pathlib import Path

import requests
from loguru import logger

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/models"
DEFAULT_OUTPUT_FILE = Path(__file__).parent / "openrouter_models.json"


def fetch_models_from_openrouter() -> list[dict]:
    """Fetch all models from OpenRouter API."""
    logger.info(f"Fetching models from {OPENROUTER_API_URL}")
    
    try:
        response = requests.get(OPENROUTER_API_URL, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        models = data.get("data", [])
        
        return models
    except requests.exceptions.RequestException:
        raise


def extract_capabilities(model: dict) -> dict:
    """Extract capability flags from model data."""
    supported_params = model.get("supported_parameters", [])
    architecture = model.get("architecture", {})
    input_modalities = architecture.get("input_modalities", [])
    
    return {
        "tool_calling": "tools" in supported_params,
        "vision": "image" in input_modalities,
        "structured_output": "structured_outputs" in supported_params or "response_format" in supported_params,
        "reasoning": "reasoning" in supported_params or "include_reasoning" in supported_params,
        "file_input": "file" in input_modalities,
        "video_input": "video" in input_modalities,
        "audio_input": "audio" in input_modalities,
    }


def extract_pricing(model: dict) -> dict:
    """Extract and convert pricing from string to float."""
    pricing_data = model.get("pricing", {})
    
    pricing = {
        "prompt": float(pricing_data.get("prompt", 0)),
        "completion": float(pricing_data.get("completion", 0)),
    }
    
    # Add web search pricing if present
    if "web_search" in pricing_data:
        pricing["web_search"] = float(pricing_data.get("web_search", 0))
    
    return pricing


def process_model(model: dict) -> dict | None:
    """Process a single model into structured format."""
    model_id = model.get("id")
    if not model_id:
        return None
    
    architecture = model.get("architecture", {})
    
    # Build model entry
    model_entry = {
        "id": model_id,
        "name": model.get("name", ""),
        "description": model.get("description", ""),
        "context_length": model.get("context_length", 0),
        "modality": architecture.get("modality", "unknown"),
        "input_modalities": architecture.get("input_modalities", []),
        "output_modalities": architecture.get("output_modalities", []),
        "capabilities": extract_capabilities(model),
        "pricing": extract_pricing(model),
        "top_provider": {
            "context_length": model.get("top_provider", {}).get("context_length"),
            "max_completion_tokens": model.get("top_provider", {}).get("max_completion_tokens"),
            "is_moderated": model.get("top_provider", {}).get("is_moderated", False),
        },
        "created": model.get("created"),
        "knowledge_cutoff": model.get("knowledge_cutoff"),
        "expiration_date": model.get("expiration_date"),
    }
    
    # Add reasoning info if present
    if "reasoning" in model and model["reasoning"]:
        model_entry["reasoning"] = {
            "mandatory": model["reasoning"].get("mandatory", False),
            "default_enabled": model["reasoning"].get("default_enabled", False),
            "supported_efforts": model["reasoning"].get("supported_efforts", []),
        }
    
    return model_entry


def generate_models_json(output_path: Path, pretty: bool = False) -> dict:
    """Generate the complete models JSON file."""
    # Fetch models
    raw_models = fetch_models_from_openrouter()
    
    # Process models
    processed_models = {}
    for model in raw_models:
        processed = process_model(model)
        if processed:
            processed_models[processed["id"]] = processed
    
    # Build output structure
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_models": len(processed_models),
        "source": OPENROUTER_API_URL,
        "models": processed_models,
    }
        
    with open(output_path, "w", encoding="utf-8") as f:
        if pretty:
            json.dump(output, f, indent=2, ensure_ascii=False)
        else:
            json.dump(output, f, ensure_ascii=False)    
    return output


def main() -> None:
    output = generate_models_json(DEFAULT_OUTPUT_FILE, pretty=True)
    logger.info(
        f"Wrote {output['total_models']} models to {DEFAULT_OUTPUT_FILE} "
        f"at {output['generated_at']}"
    )


if __name__ == "__main__":
    main()
