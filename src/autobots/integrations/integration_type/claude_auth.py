from typing import Dict, Optional, Type

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_creds.app_creds_model import ApiKeyCredsModel
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import ApiKeyAuthModel
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class ClaudeAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.claude

    @staticmethod
    async def get_api_domain(app_creds: ApiKeyCredsModel | None) -> str:
        pass

    @staticmethod
    def get_creds_model_type() -> Type[ApiKeyCredsModel]:
        return ApiKeyCredsModel

    @staticmethod
    def get_auth_model_type() -> Type[ApiKeyAuthModel]:
        return ApiKeyAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: ApiKeyCredsModel) -> ApiKeyAuthModel | Exception:
        return ApiKeyAuthModel(api_key=app_creds.api_key)

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: ApiKeyCredsModel, app_auth: ApiKeyAuthModel
    ) -> ApiKeyAuthModel | Exception:
        return app_auth if app_auth else ApiKeyAuthModel(api_key=app_creds.api_key)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: ApiKeyAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: ApiKeyAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.api_key}"}
