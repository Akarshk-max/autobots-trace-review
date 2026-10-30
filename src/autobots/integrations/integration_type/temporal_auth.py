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


class TemporalAuthModel(BaseModel):
    api_key: str


class TemporalAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.temporal

    @staticmethod
    async def get_api_domain(app_creds: TemporalAuthModel | None) -> str:
        pass

    @staticmethod
    def get_creds_model_type() -> Type[AppCredsModelType]:
        return TemporalAuthModel

    @staticmethod
    def get_auth_model_type() -> Type[IntegrationModelType]:
        return TemporalAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: TemporalAuthModel) -> TemporalAuthModel | Exception:
        return TemporalAuthModel(api_key=app_creds.api_key)

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: TemporalAuthModel, app_auth: TemporalAuthModel
    ) -> TemporalAuthModel | Exception:
        if app_auth:
            return app_auth
        return await TemporalAuth.create_auth(ctx, app_creds)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: TemporalAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: TemporalAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.api_key}"}
