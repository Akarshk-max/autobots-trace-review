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


class OpenaiAuthModel(BaseModel):
    api_key: str
    org_id: str = ""


class OpenaiAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.openai

    @staticmethod
    async def get_api_domain(app_creds: OpenaiAuthModel | None) -> str:
        pass

    @staticmethod
    def get_creds_model_type() -> Type[AppCredsModelType]:
        return OpenaiAuthModel

    @staticmethod
    def get_auth_model_type() -> Type[IntegrationModelType]:
        return OpenaiAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: OpenaiAuthModel) -> OpenaiAuthModel | Exception:
        return OpenaiAuthModel(api_key=app_creds.api_key, org_id=app_creds.org_id)

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: OpenaiAuthModel, app_auth: OpenaiAuthModel
    ) -> OpenaiAuthModel | Exception:
        return (
            app_auth
            if app_auth
            else OpenaiAuthModel(api_key=app_creds.api_key, org_id=app_creds.org_id)
        )

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: ApiKeyAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: ApiKeyAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.api_key}"}
