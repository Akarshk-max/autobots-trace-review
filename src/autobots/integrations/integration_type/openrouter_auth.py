from typing import Dict, Optional, Type

from pydantic import BaseModel

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.conn.openrouter.openrouter_client import OPENROUTER_BASE_URL
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import (
    AppCredsModelType,
    IntegrationModelType,
    IntegrationTypeABC,
)


class OpenRouterAuthModel(BaseModel):
    api_key: str

    # Optional OpenRouter attribution headers
    http_referer: str = "https://www.runkai.ai/"
    app_title: str = "autobots"


class OpenRouterAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.openrouter

    @staticmethod
    async def get_api_domain(app_creds: OpenRouterAuthModel | None) -> str:
        return OPENROUTER_BASE_URL

    @staticmethod
    def get_creds_model_type() -> Type[AppCredsModelType]:
        return OpenRouterAuthModel

    @staticmethod
    def get_auth_model_type() -> Type[IntegrationModelType]:
        return OpenRouterAuthModel

    @staticmethod
    async def create_auth(
        ctx: Context,
        app_creds: OpenRouterAuthModel,
    ) -> OpenRouterAuthModel | Exception:
        return OpenRouterAuthModel(
            api_key=app_creds.api_key,
            http_referer=app_creds.http_referer,
            app_title=app_creds.app_title,
        )

    @staticmethod
    async def refresh_auth(
        ctx: Context,
        app_creds: OpenRouterAuthModel,
        app_auth: OpenRouterAuthModel,
    ) -> OpenRouterAuthModel | Exception:
        return app_auth or OpenRouterAuthModel(
            api_key=app_creds.api_key,
            http_referer=app_creds.http_referer,
            app_title=app_creds.app_title,
        )

    @staticmethod
    async def is_refresh_required(
        ctx: Context,
        app_auth: OpenRouterAuthModel,
    ) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context,
        app_auth: OpenRouterAuthModel,
        config: Optional[APIRequest] = None,
    ) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {app_auth.api_key}",
        }

        if app_auth.http_referer:
            headers["HTTP-Referer"] = app_auth.http_referer

        if app_auth.app_title:
            headers["X-OpenRouter-Title"] = app_auth.app_title

        return headers