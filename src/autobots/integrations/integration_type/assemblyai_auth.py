from typing import Dict, Optional, Type

from pydantic import BaseModel

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class AssemblyAiApiKeyAuthModel(BaseModel):
    api_key: str


class AssemblyAiApikeyCredsModel(BaseModel):
    api_key: str


class AssemblyAiAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.assemblyai

    @staticmethod
    async def get_api_domain(app_creds: AssemblyAiApikeyCredsModel | None) -> str:
        pass

    @staticmethod
    def get_creds_model_type() -> Type[AssemblyAiApikeyCredsModel]:
        return AssemblyAiApikeyCredsModel

    @staticmethod
    def get_auth_model_type() -> Type[AssemblyAiApiKeyAuthModel]:
        return AssemblyAiApiKeyAuthModel

    @staticmethod
    async def create_auth(
        ctx: Context, app_creds: AssemblyAiApikeyCredsModel
    ) -> AssemblyAiApiKeyAuthModel | Exception:
        return AssemblyAiApiKeyAuthModel(api_key=app_creds.api_key)

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: AssemblyAiApikeyCredsModel, app_auth: AssemblyAiApiKeyAuthModel
    ) -> AssemblyAiApiKeyAuthModel | Exception:
        if app_auth:
            return app_auth
        return await AssemblyAiAuth.create_auth(ctx, app_creds)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: AssemblyAiApiKeyAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: AssemblyAiApiKeyAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.api_key}"}
