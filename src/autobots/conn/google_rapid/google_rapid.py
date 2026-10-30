import httpx

from src.autobots.action.action_type.action_multimodal.action_multimodal_google_search.action_multimodal_google_search_model import (
    ActionMultimodalGoogleSearchConfig,
)
from src.autobots.core.settings import Settings, SettingsProvider


async def get_google_rapid(
    action_config: ActionMultimodalGoogleSearchConfig, settings: Settings = SettingsProvider.sget()
):
    headers = {
        "x-rapidapi-key": settings.RAPID_API_KEY,
        "x-rapidapi-host": "google-api31.p.rapidapi.com",
        "Content-Type": "application/json",
    }
    data = {
        "text": action_config.q,
        "safesearch": "off",
        "timelimit": "",
        "region": "wt-wt",
        "max_results": 20,
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://google-api31.p.rapidapi.com/websearch", headers=headers, json=data
        )
        response.raise_for_status()
        return response.json()
