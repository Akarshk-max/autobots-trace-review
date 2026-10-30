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


class AzureOpenAIAuthModel(BaseModel):
    api_key: str
    endpoint: str
    api_version: str = "2025-03-01-preview"
    # For regular actions the user picks the model/deployment per-action, so no
    # stored default is needed there. But internal system features (e.g. chat
    # title generation) run without a user-authored config and need some
    # deployment to call. Azure's data-plane API (api_key + endpoint only) can't
    # enumerate the customer's actual deployment names — client.models.list()
    # returns the underlying base model IDs (e.g. "gpt-4o-mini"), not deployment
    # aliases (e.g. "my-gpt4-prod") — so ask for a cheap deployment explicitly,
    # scoped to this one purpose.
    title_gen_deployment: str = ""


class AzureOpenAIAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.azure_openai

    @staticmethod
    async def get_api_domain(app_creds: AzureOpenAIAuthModel | None) -> str:
        pass

    @staticmethod
    def get_creds_model_type() -> Type[AppCredsModelType]:
        return AzureOpenAIAuthModel

    @staticmethod
    def get_auth_model_type() -> Type[IntegrationModelType]:
        return AzureOpenAIAuthModel

    @staticmethod
    async def create_auth(
        ctx: Context, app_creds: AzureOpenAIAuthModel
    ) -> AzureOpenAIAuthModel | Exception:
        return AzureOpenAIAuthModel(
            api_key=app_creds.api_key,
            endpoint=app_creds.endpoint,
            api_version=app_creds.api_version,
            title_gen_deployment=app_creds.title_gen_deployment,
        )

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: AzureOpenAIAuthModel, app_auth: AzureOpenAIAuthModel
    ) -> AzureOpenAIAuthModel | Exception:
        return app_auth if app_auth else await AzureOpenAIAuth.create_auth(ctx, app_creds)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: ApiKeyAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: AzureOpenAIAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"api-key": app_auth.api_key}
