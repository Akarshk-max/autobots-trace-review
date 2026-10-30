import urllib.parse
from typing import Dict, Optional, Type

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots import SettingsProvider
from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.api.v1.api_auth.oauth_state import new_oauth_state
from src.autobots.conn.only_auth.smartsheet.smartsheet_client import (
    Smartsheet,
    SmartsheetAccessTokenRq,
    SmartsheetRefreshTokenRq,
)
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import (
    IntegrationConnectResolved,
    IntegrationConnectResult,
    OAuthModel,
)
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC
from src.autobots.user.user_orm_model import UserORM


class SmartsheetCreds(BaseModel):
    pass


class SmartsheetAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.smartsheet

    @staticmethod
    def oauth_callback_path() -> str:
        """Where Smartsheet sends the browser back. Registered in its console."""
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}/smartsheet/oauth-callback"

    @staticmethod
    def build_authorize_url(
        ctx: Context, name: str, callback_url: str, post_auth_uri: str
    ) -> str:
        """The consent URL, built here so the route and connect() cannot drift.

        `callback_url` is ours and is what the provider matches against its registration;
        `post_auth_uri` is where the user lands afterwards and rides inside signed state.
        """
        state_b64 = new_oauth_state(ctx=ctx, name=name, redirect_uri=post_auth_uri).to_base64()
        params = {
            "response_type": "code",
            "client_id": SettingsProvider.sget().SMARTSHEET_CLIENT_ID,
            "redirect_uri": callback_url,
            "scope": SettingsProvider.sget().SMARTSHEET_SCOPES,
            "state": state_b64,
        }
        return "https://app.smartsheet.com/b/authorize?" + urllib.parse.urlencode(params)

    @staticmethod
    async def connect(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        req: IntegrationConnectResolved,
        decrypt: bool = False,
    ) -> IntegrationConnectResult:
        """No creds to turn into auth — the user has to visit the provider."""
        return IntegrationConnectResult(
            redirect_url=SmartsheetAuth.build_authorize_url(
                ctx=ctx,
                name=req.name,
                callback_url=req.callback_url or "",
                post_auth_uri=req.post_auth_uri or "",
            )
        )

    @staticmethod
    async def get_api_domain(app_creds: SmartsheetCreds | None) -> str:
        return "https://api.smartsheet.com"

    @staticmethod
    def get_creds_model_type() -> Type[SmartsheetCreds]:
        return SmartsheetCreds

    @staticmethod
    def get_auth_model_type() -> Type[OAuthModel]:
        return OAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: SmartsheetAccessTokenRq) -> OAuthModel | Exception:
        try:
            access_token = await Smartsheet.access_token(ctx=ctx, rq=app_creds)
            if isinstance(access_token, Exception):
                return access_token

            return OAuthModel(
                access_token=access_token.access_token,
                refresh_token=access_token.refresh_token,
                expires_in=access_token.expires_in,
                token_type=access_token.token_type,
            )
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error creating Smartsheet auth: {str(e)}")
            return e

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: SmartsheetCreds, app_auth: OAuthModel
    ) -> OAuthModel | Exception:
        try:
            refresh_rq = SmartsheetRefreshTokenRq(refresh_token=app_auth.refresh_token)
            refreshed = await Smartsheet.refresh_access_token(ctx=ctx, rq=refresh_rq)
            if isinstance(refreshed, Exception):
                return refreshed

            return OAuthModel(
                access_token=refreshed.access_token,
                refresh_token=refreshed.refresh_token or app_auth.refresh_token,
                expires_in=refreshed.expires_in,
                token_type=refreshed.token_type,
            )
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error refreshing Smartsheet auth: {str(e)}")
            return e

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: OAuthModel) -> bool:
        curr_time = Clock.datetime_now()
        duration_sec = (curr_time - app_auth.created_at).total_seconds()
        is_refresh = duration_sec > app_auth.expires_in - 300  # 5-min buffer
        logger.bind(ctx=ctx, is_refresh_required=is_refresh).info("Smartsheet is_refresh_required")
        return is_refresh

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: OAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.access_token}"}
