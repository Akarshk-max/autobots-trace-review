from datetime import datetime
from typing import Dict, Literal, Optional, Type

from loguru import logger
from pydantic import BaseModel, Field

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.conn.only_auth.zoho.zoho import Zoho
from src.autobots.conn.only_auth.zoho.zoho_model import (
    ZohoTokenGenerateRequest,
    ZohoTokenRefreshRequest,
)
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class OAuthCredsZohoModel(ZohoTokenGenerateRequest):
    api_domain: Literal[
        "https://www.zohoapis.ca",
        "https://www.zohoapis.eu",
        "https://www.zohoapis.com.cn",
        "https://www.zohoapis.com",
        "https://www.zohoapis.com.au",
        "https://www.zohoapis.in",
        "https://www.zohoapis.jp",
    ] = "https://www.zohoapis.com"


class OAuthZohoModel(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: float
    token_type: str
    created_at: datetime = Field(default_factory=lambda: Clock.datetime_now())
    api_domain: str


class ZohoAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.zoho

    @staticmethod
    async def get_api_domain(app_creds: OAuthCredsZohoModel | None) -> str:
        pass

    @staticmethod
    def get_creds_model_type() -> Type[OAuthCredsZohoModel]:
        return OAuthCredsZohoModel

    @staticmethod
    def get_auth_model_type() -> Type[OAuthZohoModel]:
        return OAuthZohoModel

    @staticmethod
    async def create_auth(
        ctx: Context, app_creds: OAuthCredsZohoModel
    ) -> OAuthZohoModel | Exception:
        req = ZohoTokenGenerateRequest(
            api_domain=app_creds.api_domain,
            client_id=app_creds.client_id,
            client_secret=app_creds.client_secret,
            auth_code=app_creds.auth_code,
        )
        res = await Zoho.generate_token(req=req)
        return OAuthZohoModel(
            access_token=res.access_token,
            refresh_token=res.refresh_token,
            expires_in=res.expires_in,
            token_type=res.token_type,
            api_domain=app_creds.api_domain,
        )

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: OAuthCredsZohoModel, app_auth: OAuthZohoModel
    ) -> OAuthZohoModel | Exception:
        # if not ZohoAuth.is_refresh_required(ctx, app_auth):
        #     return app_auth
        req = ZohoTokenRefreshRequest(
            api_domain=app_auth.api_domain,
            client_id=app_creds.client_id,
            client_secret=app_creds.client_secret,
            refresh_token=app_auth.refresh_token,
        )
        res = await Zoho.refresh_token(req=req)
        return OAuthZohoModel(
            access_token=res.access_token,
            refresh_token=res.refresh_token,
            expires_in=res.expires_in,
            token_type=res.token_type,
            api_domain=app_creds.api_domain,
        )

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: OAuthZohoModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.access_token}"}

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: OAuthZohoModel) -> bool:
        curr_time = Clock.datetime_now()
        auth_creation_time = app_auth.created_at
        duration_since_creation = curr_time - auth_creation_time
        duration_sec = duration_since_creation.total_seconds()
        is_refresh = duration_sec > app_auth.expires_in - 600
        logger.bind(
            ctx=ctx,
            is_refresh_required=is_refresh,
            curr_time=str(curr_time),
            auth_creation_time=str(auth_creation_time),
            duration_sec=duration_sec,
        ).info("is_refresh_required")
        return is_refresh
