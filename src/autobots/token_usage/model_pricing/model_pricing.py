import json
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

from src.autobots.core.logging.app_code import AppCode
from src.autobots.token_usage.token_usage_doc_model import LlmCallUsage, LlmProvider

PRICES_PATH = Path(__file__).with_name("model_prices.json")
TOKENS_PER_PRICE_UNIT = 1_000_000
NAME_BOUNDARY = ("-", "@", ":")


def _load_prices() -> Dict[str, Dict[str, Any]]:
    with PRICES_PATH.open() as prices_file:
        return json.load(prices_file)["models"]


MODEL_PRICES: Dict[str, Dict[str, Any]] = _load_prices()

_unpriced_models: set[str] = set()


def price_for(provider: LlmProvider, model: str) -> Optional[Dict[str, Any]]:
    name = model.rsplit("/", 1)[-1].lower()
    matches = [
        key
        for key, price in MODEL_PRICES.items()
        if price["provider"] == provider.value
        and name.startswith(key)
        and (len(name) == len(key) or name[len(key)] in NAME_BOUNDARY)
    ]
    if not matches:
        return None
    return MODEL_PRICES[max(matches, key=len)]


def cost_for(provider: LlmProvider, call: LlmCallUsage) -> Optional[float]:
    price = price_for(provider, call.model)
    if price is None:
        if call.model not in _unpriced_models:
            _unpriced_models.add(call.model)
            logger.bind(code=AppCode.TOKEN_USAGE, provider=provider.value, model=call.model).warning(
                "No price for model, cost left unreported"
            )
        return None

    tier = price.get("long_context")
    if tier and call.input_tokens + call.cached_input_tokens >= tier["min_input_tokens"]:
        price = {**price, **tier}

    priced_tokens = (
        call.input_tokens * price["input"]
        + call.cached_input_tokens * price.get("cached_input", 0)
        + call.cache_write_tokens * price.get("cache_write", 0)
        + call.output_tokens * price["output"]
    )
    return priced_tokens / TOKENS_PER_PRICE_UNIT
