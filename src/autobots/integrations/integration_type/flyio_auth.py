from typing import Dict, Optional, Type

from pydantic import BaseModel

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class FlyioApiKeyCredsModel(BaseModel):
    token: str


class FlyioApiKeyAuthModel(BaseModel):
    token: str

class FlyioAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.flyio

    @staticmethod
    async def get_api_domain(app_creds: FlyioApiKeyCredsModel | None) -> str:
        pass

    @staticmethod
    def get_creds_model_type() -> Type[FlyioApiKeyCredsModel]:
        return FlyioApiKeyCredsModel

    @staticmethod
    def get_auth_model_type() -> Type[FlyioApiKeyAuthModel]:
        return FlyioApiKeyAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: FlyioApiKeyCredsModel) -> FlyioApiKeyAuthModel | Exception:
        return FlyioApiKeyAuthModel(token=app_creds.token)

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: FlyioApiKeyCredsModel, app_auth: FlyioApiKeyAuthModel
    ) -> FlyioApiKeyAuthModel | Exception:
        return app_auth if app_auth else FlyioApiKeyAuthModel(token=app_creds.token)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: FlyioApiKeyAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: FlyioApiKeyAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {app_auth.token}",
            "Content-Type": "application/json",
        }
