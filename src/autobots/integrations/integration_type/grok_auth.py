from typing import Dict, Optional, Type

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_creds.app_creds_model import GrokApikeyCredsModel
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import GrokApiKeyAuthModel
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class GrokAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.grok

    @staticmethod
    async def get_api_domain(app_creds: GrokApikeyCredsModel | None) -> str:
        pass

    @staticmethod
    def get_creds_model_type() -> Type[GrokApikeyCredsModel]:
        return GrokApikeyCredsModel

    @staticmethod
    def get_auth_model_type() -> Type[GrokApiKeyAuthModel]:
        return GrokApiKeyAuthModel

    @staticmethod
    async def create_auth(
        ctx: Context, app_creds: GrokApikeyCredsModel
    ) -> GrokApiKeyAuthModel | Exception:
        return GrokApiKeyAuthModel(
            api_key=app_creds.api_key, management_api_key=app_creds.management_api_key
        )

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: GrokApikeyCredsModel, app_auth: GrokApiKeyAuthModel
    ) -> GrokApiKeyAuthModel | Exception:
        if app_auth:
            return app_auth
        return await GrokAuth.create_auth(ctx, app_creds)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: GrokApiKeyAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: GrokApiKeyAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.api_key}"}
