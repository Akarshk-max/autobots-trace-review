import base64
from datetime import datetime
from typing import Dict, Optional, Type

from loguru import logger
from pydantic import BaseModel, Field

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class SAPBasicAuthModel(BaseModel):
    username: str
    password: str
    created_at: datetime = Field(default_factory=lambda: Clock.datetime_now())


class SAPBasicAuthCreds(BaseModel):
    username: str
    password: str
    host: str
    port: str


class SAPAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.sap

    @staticmethod
    async def get_api_domain(app_creds: SAPBasicAuthCreds | None) -> str:
        if app_creds:
            return f"https://{app_creds.host}:{app_creds.port}"
        return ""

    @staticmethod
    def get_creds_model_type() -> Type[SAPBasicAuthCreds]:
        return SAPBasicAuthCreds

    @staticmethod
    def get_auth_model_type() -> Type[SAPBasicAuthModel]:
        return SAPBasicAuthModel

    @staticmethod
    async def create_auth(
        ctx: Context, app_creds: SAPBasicAuthCreds
    ) -> SAPBasicAuthModel | Exception:
        try:
            logger.bind(ctx=ctx).info("Creating SAP Basic authentication")
            return SAPBasicAuthModel(username=app_creds.username, password=app_creds.password)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error creating SAP auth: {str(e)}")
            return e

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: SAPBasicAuthCreds | None, app_auth: SAPBasicAuthModel | None
    ) -> SAPBasicAuthModel | Exception:
        try:
            logger.bind(ctx=ctx).info("Refreshing SAP Basic authentication (no-op)")
            return await SAPAuth.create_auth(ctx, app_creds)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error refreshing SAP auth: {str(e)}")
            return e

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: SAPBasicAuthModel) -> bool:
        logger.bind(ctx=ctx).info("Checking if SAP refresh required (always False)")
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: SAPBasicAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        try:
            logger.bind(ctx=ctx).info("Generating SAP Basic auth header")
            credentials = f"{app_auth.username}:{app_auth.password}"
            encoded = base64.b64encode(credentials.encode()).decode()
            return {"Authorization": f"Basic {encoded}"}
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error generating SAP auth header: {str(e)}")
            return {}

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: SAPBasicAuthCreds, app_auth: SAPBasicAuthModel, url: str
    ) -> str:
        try:
            logger.bind(ctx=ctx, original_url=url).info("Updating SAP URL")
            new_url = url.replace("{host}", app_creds.host).replace("{port}", app_creds.port)
            logger.bind(ctx=ctx, updated_url=new_url).info("SAP URL updated successfully")
            return new_url
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error updating SAP URL: {str(e)}")
            return url
