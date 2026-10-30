from httpx import Timeout
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict

from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.context import Context

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterConfig(BaseModel):
    api_key: str
    timeout: float | Timeout = 900

    http_referer: str | None = None
    app_title: str | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)


class OpenRouter:
    def __init__(self, *, config: OpenRouterConfig, is_system_creds: bool = False):
        default_headers: dict[str, str] = {}

        if config.http_referer:
            default_headers["HTTP-Referer"] = config.http_referer

        if config.app_title:
            default_headers["X-OpenRouter-Title"] = config.app_title

        self.client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=OPENROUTER_BASE_URL,
            timeout=config.timeout,
            default_headers=default_headers or None,
        )
        self.is_system_creds = is_system_creds

def get_conn_openrouter(
    ctx: Context,
    api_key: str,
    http_referer: str | None = None,
    app_title: str | None = None,
    settings: Settings = SettingsProvider.sget(),
    is_system_creds: bool = False,
) -> OpenRouter:
    return OpenRouter(
        config=OpenRouterConfig(
            api_key=api_key,
            timeout=settings.OPENAI_TIMEOUT,
            http_referer=http_referer,
            app_title=app_title,
        ),
        is_system_creds=is_system_creds,
    )