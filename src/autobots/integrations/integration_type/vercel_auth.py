from typing import Dict, Optional, Type

from pydantic import BaseModel

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import ApiKeyAuthModel
from src.autobots.integrations.integration_type.integration_type import (
    AppCredsModelType,
    IntegrationModelType,
    IntegrationTypeABC,
)


class VercelAuthModel(BaseModel):
    api_key: str


class VercelAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.vercel

    @staticmethod
    async def get_api_domain(app_creds: VercelAuthModel | None) -> str:
        return "vercel.com"

    @staticmethod
    def get_creds_model_type() -> Type[AppCredsModelType]:
        return VercelAuthModel

    @staticmethod
    def get_auth_model_type() -> Type[IntegrationModelType]:
        return VercelAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: VercelAuthModel) -> VercelAuthModel | Exception:
        return VercelAuthModel(api_key=app_creds.api_key)

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: VercelAuthModel, app_auth: VercelAuthModel
    ) -> VercelAuthModel | Exception:
        return app_auth if app_auth else VercelAuthModel(api_key=app_creds.api_key)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: ApiKeyAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: ApiKeyAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.api_key}"}
