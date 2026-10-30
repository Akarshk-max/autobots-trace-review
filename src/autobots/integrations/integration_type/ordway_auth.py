from enum import Enum
from typing import Dict, Optional, Type

from loguru import logger
from pydantic import BaseModel, HttpUrl

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import (
    AppCredsModelType,
    IntegrationModelType,
    IntegrationTypeABC,
)


class OrdwayEnvironment(str, Enum):
    sandbox = "sandbox"
    production = "production"


class OrdwayAuthModel(BaseModel):
    user_email: str
    user_token: str
    api_key: str
    company: str
    environment: OrdwayEnvironment = OrdwayEnvironment.production


class OrdwayAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.ordway

    @staticmethod
    async def get_api_domain(app_creds: OrdwayAuthModel | None) -> str:
        if app_creds and app_creds.environment == OrdwayEnvironment.production:
            return "https://api.ordwaylabs.com/api/v1"
        return "https://api.sandbox.ordwaylabs.com/api/v1"

    @staticmethod
    def get_creds_model_type() -> Type[AppCredsModelType]:
        return OrdwayAuthModel

    @staticmethod
    def get_auth_model_type() -> Type[IntegrationModelType]:
        return OrdwayAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: OrdwayAuthModel) -> OrdwayAuthModel | Exception:
        return OrdwayAuthModel(
            user_email=app_creds.user_email,
            user_token=app_creds.user_token,
            api_key=app_creds.api_key,
            company=app_creds.company,
            environment=app_creds.environment,
        )

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: OrdwayAuthModel, app_auth: OrdwayAuthModel
    ) -> OrdwayAuthModel | Exception:
        return (
            app_auth
            if app_auth
            else OrdwayAuthModel(
                user_email=app_creds.user_email,
                user_token=app_creds.user_token,
                api_key=app_creds.api_key,
                company=app_creds.company,
                environment=app_creds.environment,
            )
        )

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: OrdwayAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: OrdwayAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {
            "X-User-Company": app_auth.company,
            "X-User-Email": app_auth.user_email,
            "X-User-Token": app_auth.user_token,
            "X-API-KEY": app_auth.api_key,
            "Content-Type": "application/json",
        }

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: OrdwayAuthModel, app_auth: OrdwayAuthModel, url: str
    ) -> str:
        logger.bind(ctx=ctx, original_url=url, environment=app_creds.environment).info("Updating Ordway URL")
        base_domain = await OrdwayAuth.get_api_domain(app_creds)
        
        if "/api/v1" in url:
            endpoint_path = url.split("/api/v1", 1)[1]
            updated_url = f"{base_domain}{endpoint_path}"
            logger.bind(ctx=ctx, updated_url=updated_url).info("Updated Ordway URL with /api/v1 split")
            return updated_url
        
        http_url = HttpUrl(url)
        updated_url = url.replace(f"{http_url.scheme}://{http_url.host}", base_domain)
        logger.bind(ctx=ctx, updated_url=updated_url).info("Updated Ordway URL with host replacement")
        return updated_url
