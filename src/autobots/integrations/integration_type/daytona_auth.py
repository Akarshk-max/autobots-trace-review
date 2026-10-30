# src/autobots/integrations/integration_type/daytona_auth.py
from typing import Dict, Optional, Type

from pydantic import BaseModel

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class DaytonaApiKeyCredsModel(BaseModel):
    api_key: str
    api_url: str = "https://app.daytona.io/api"


class DaytonaApiKeyAuthModel(BaseModel):
    api_key: str
    api_url: str = "https://app.daytona.io/api"


class DaytonaAuth(IntegrationTypeABC[DaytonaApiKeyCredsModel, DaytonaApiKeyAuthModel]):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.daytona

    @staticmethod
    async def get_api_domain(app_creds: DaytonaApiKeyCredsModel | None) -> str:
        return app_creds.api_url if app_creds and app_creds.api_url else "https://app.daytona.io/api"

    @staticmethod
    def get_creds_model_type() -> Type[DaytonaApiKeyCredsModel]:
        return DaytonaApiKeyCredsModel

    @staticmethod
    def get_auth_model_type() -> Type[DaytonaApiKeyAuthModel]:
        return DaytonaApiKeyAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: DaytonaApiKeyCredsModel) -> DaytonaApiKeyAuthModel | Exception:
        # API key apps have no refresh lifecycle; auth mirrors creds.
        return DaytonaApiKeyAuthModel(api_key=app_creds.api_key, api_url=app_creds.api_url)

    @staticmethod
    async def refresh_auth(
        ctx: Context,
        app_creds: DaytonaApiKeyCredsModel | None,
        app_auth: DaytonaApiKeyAuthModel | None,
    ) -> DaytonaApiKeyAuthModel | Exception:
        # No refresh needed; return existing or derive from creds.
        if app_auth:
            return app_auth
        assert app_creds is not None
        return DaytonaApiKeyAuthModel(api_key=app_creds.api_key, api_url=app_creds.api_url)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: DaytonaApiKeyAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: DaytonaApiKeyAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {app_auth.api_key}",
            "Content-Type": "application/json",
        }
