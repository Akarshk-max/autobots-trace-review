from typing import Dict, Optional, Type

from pydantic import BaseModel

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_creds.app_creds_model import ApiKeyCredsModel
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class BluedotAuthModel(BaseModel):
    api_key: str


class BluedotAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.bluedot

    @staticmethod
    async def get_api_domain(app_creds: ApiKeyCredsModel | None) -> str:
        return "https://config.bluedot.io"

    @staticmethod
    def get_creds_model_type() -> Type[ApiKeyCredsModel]:
        return ApiKeyCredsModel

    @staticmethod
    def get_auth_model_type() -> Type[BluedotAuthModel]:
        return BluedotAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: ApiKeyCredsModel) -> BluedotAuthModel | Exception:
        return BluedotAuthModel(api_key=app_creds.api_key)

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: ApiKeyCredsModel, app_auth: BluedotAuthModel
    ) -> BluedotAuthModel | Exception:
        return app_auth if app_auth else BluedotAuthModel(api_key=app_creds.api_key)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: BluedotAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: BluedotAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"x-bluedot-api-key": app_auth.api_key}
