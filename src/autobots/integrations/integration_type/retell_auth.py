from typing import Dict, Optional, Type

from pydantic import BaseModel

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import (
    AppCredsModelType,
    IntegrationModelType,
    IntegrationTypeABC,
)

RETELL_API_BASE_URL = "https://api.retellai.com"


class RetellAuthModel(BaseModel):
    api_key: str


class RetellAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.retell

    @staticmethod
    async def get_api_domain(app_creds: RetellAuthModel | None) -> str:
        return RETELL_API_BASE_URL

    @staticmethod
    def get_creds_model_type() -> Type[AppCredsModelType]:
        return RetellAuthModel

    @staticmethod
    def get_auth_model_type() -> Type[IntegrationModelType]:
        return RetellAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: RetellAuthModel) -> RetellAuthModel | Exception:
        return RetellAuthModel(api_key=app_creds.api_key)

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: RetellAuthModel, app_auth: RetellAuthModel
    ) -> RetellAuthModel | Exception:
        return app_auth if app_auth else RetellAuthModel(api_key=app_creds.api_key)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: RetellAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: RetellAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.api_key}"}
