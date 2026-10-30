from typing import Dict, Optional, Type
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import (
    AppCredsModelType,
    IntegrationModelType,
    IntegrationTypeABC,
)

NANGO_API_BASE_URL = "https://api.nango.dev"


class NangoAuthModel(BaseModel):
    """Connect to Nango.

    Api Key is your Nango API key - an Environment key for environment-scoped
    endpoints, or an Account key for account-level ones.

    Api Domain only needs changing if you self-host Nango; leave it as
    https://api.nango.dev for Nango Cloud.
    """

    api_key: str
    api_domain: str = NANGO_API_BASE_URL


class NangoAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.nango

    @staticmethod
    async def get_api_domain(app_creds: NangoAuthModel | None) -> str:
        if not app_creds or not app_creds.api_domain:
            return NANGO_API_BASE_URL
        return app_creds.api_domain.rstrip("/")

    @staticmethod
    def get_creds_model_type() -> Type[AppCredsModelType]:
        return NangoAuthModel

    @staticmethod
    def get_auth_model_type() -> Type[IntegrationModelType]:
        return NangoAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: NangoAuthModel) -> NangoAuthModel | Exception:
        return NangoAuthModel(api_key=app_creds.api_key, api_domain=app_creds.api_domain)

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: NangoAuthModel, app_auth: NangoAuthModel
    ) -> NangoAuthModel | Exception:
        return app_auth if app_auth else NangoAuthModel(
            api_key=app_creds.api_key, api_domain=app_creds.api_domain
        )

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: NangoAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: NangoAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.api_key}"}

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: NangoAuthModel, app_auth: NangoAuthModel, url: str
    ) -> str:
        domain = await NangoAuth.get_api_domain(app_auth or app_creds)
        base = urlsplit(domain)
        parts = urlsplit(url)
        path = f"{base.path.rstrip('/')}{parts.path}"
        return urlunsplit((base.scheme, base.netloc, path, parts.query, parts.fragment))
