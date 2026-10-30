import base64
from typing import Dict, Optional, Type

from pydantic import BaseModel, HttpUrl

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class ChargebeeCredsModel(BaseModel):
    """Chargebee credentials: API key and site-specific domain."""

    api_key: str
    api_domain: str  # e.g. https://mysite.chargebee.com


class ChargebeeAuthModel(BaseModel):
    """Chargebee auth: Basic token (api_key: as username, empty password)."""

    basic_token: str
    api_domain: str


# Chargebee uses HTTP Basic auth: username=API key, password=empty
# https://apidocs.chargebee.com/docs/api/auth
class ChargebeeAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.chargebee

    @staticmethod
    async def get_api_domain(app_creds: ChargebeeCredsModel | None) -> str:
        if not app_creds:
            return ""
        domain = app_creds.api_domain.rstrip("/")
        return f"{domain}/api/v2" if not domain.endswith("/api/v2") else domain

    @staticmethod
    def get_creds_model_type() -> Type[ChargebeeCredsModel]:
        return ChargebeeCredsModel

    @staticmethod
    def get_auth_model_type() -> Type[ChargebeeAuthModel]:
        return ChargebeeAuthModel

    @staticmethod
    async def create_auth(
        ctx: Context, app_creds: ChargebeeCredsModel
    ) -> ChargebeeAuthModel | Exception:
        assert HttpUrl(app_creds.api_domain)
        # Chargebee: username=api_key, password=empty
        basic_auth_bytes = f"{app_creds.api_key}:".encode("utf-8")
        basic_token = base64.b64encode(basic_auth_bytes).decode("utf-8")
        return ChargebeeAuthModel(
            basic_token=basic_token, api_domain=app_creds.api_domain.rstrip("/")
        )

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: ChargebeeCredsModel, app_auth: ChargebeeAuthModel
    ) -> ChargebeeAuthModel | Exception:
        return await ChargebeeAuth.create_auth(ctx=ctx, app_creds=app_creds)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: ChargebeeAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: ChargebeeAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Basic {app_auth.basic_token}"}
