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


class VertexAIAuthModel(BaseModel):
    project_id: str
    region: str = "us-central1"
    service_account_json: str  # full GCP service account JSON string


class VertexAIAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.vertex_ai

    @staticmethod
    async def get_api_domain(app_creds: VertexAIAuthModel | None) -> str:
        pass

    @staticmethod
    def get_creds_model_type() -> Type[AppCredsModelType]:
        return VertexAIAuthModel

    @staticmethod
    def get_auth_model_type() -> Type[IntegrationModelType]:
        return VertexAIAuthModel

    @staticmethod
    async def create_auth(
        ctx: Context, app_creds: VertexAIAuthModel
    ) -> VertexAIAuthModel | Exception:
        return VertexAIAuthModel(
            project_id=app_creds.project_id,
            region=app_creds.region,
            service_account_json=app_creds.service_account_json,
        )

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: VertexAIAuthModel, app_auth: VertexAIAuthModel
    ) -> VertexAIAuthModel | Exception:
        return app_auth if app_auth else await VertexAIAuth.create_auth(ctx, app_creds)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: ApiKeyAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: VertexAIAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {}
