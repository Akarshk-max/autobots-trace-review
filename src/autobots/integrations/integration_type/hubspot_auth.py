import urllib.parse
from typing import Dict, Optional, Type

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, HttpUrl

from src.autobots import SettingsProvider
from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.api.v1.api_auth.oauth_state import new_oauth_state
from src.autobots.conn.only_auth.hubspot.hubspot_client import (
    HubSpot,
    HubSpotAccessTokenRq,
    HubSpotRefreshTokenRq,
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


class HubSpotCreds(BaseModel):
    pass


class HubSpotAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.hubspot

    @staticmethod
    def oauth_callback_path() -> str:
        """Where the provider sends the browser back. Registered in its console."""
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}/hubspot/oauth-callback"

    @staticmethod
    def build_authorize_url(
        ctx: Context, name: str, callback_url: str, post_auth_uri: str
    ) -> str:
        """The consent URL, built here so the route and connect() cannot drift.

        `callback_url` is ours and is what the provider matches against its registration;
        `post_auth_uri` is where the user lands afterwards and rides inside signed state.
        """
        client_id = SettingsProvider.sget().HUBSPOT_CLIENT_ID
        scopes = SettingsProvider.sget().HUBSPOT_SCOPES
        state_b64 = new_oauth_state(ctx=ctx, name=name, redirect_uri=post_auth_uri).to_base64()
        url_encoded_callback_uri = urllib.parse.quote_plus(callback_url)
        # HubSpot doesn't need response_type=code (unlike other OAuth services)
        oauth_url = (
            "https://app.hubspot.com/oauth/authorize?"
            f"client_id={client_id}&"
            f"redirect_uri={url_encoded_callback_uri}&"
            f"scope={scopes}&"
            f"state={urllib.parse.quote_plus(state_b64)}"
        )
        return oauth_url

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
            redirect_url=HubSpotAuth.build_authorize_url(
                ctx=ctx,
                name=req.name,
                callback_url=req.callback_url or "",
                post_auth_uri=req.post_auth_uri or "",
            )
        )

    @staticmethod
    async def get_api_domain(app_creds: HubSpotCreds | None):
        return "https://api.hubapi.com"

    @staticmethod
    def get_creds_model_type() -> Type[HubSpotCreds]:
        return HubSpotCreds

    @staticmethod
    def get_auth_model_type() -> Type[OAuthModel]:
        return OAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: HubSpotAccessTokenRq) -> OAuthModel | Exception:
        try:
            access_token = await HubSpot.access_token(ctx=ctx, rq=app_creds)
            if isinstance(access_token, Exception):
                return access_token

            oauth_model = OAuthModel(
                access_token=access_token.access_token,
                refresh_token=access_token.refresh_token,
                expires_in=access_token.expires_in,
                token_type=access_token.token_type,
            )
            return oauth_model
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error creating HubSpot auth: {str(e)}")
            return e

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: HubSpotCreds, app_auth: OAuthModel
    ) -> OAuthModel | Exception:
        try:
            refresh_rq = HubSpotRefreshTokenRq(refresh_token=app_auth.refresh_token)
            refreshed_token = await HubSpot.refresh_access_token(ctx=ctx, rq=refresh_rq)
            if isinstance(refreshed_token, Exception):
                return refreshed_token

            oauth_model = OAuthModel(
                access_token=refreshed_token.access_token,
                refresh_token=app_auth.refresh_token,  # Keep existing refresh token
                expires_in=refreshed_token.expires_in,
                token_type=refreshed_token.token_type,
            )
            return oauth_model
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error refreshing HubSpot auth: {str(e)}")
            return e

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: OAuthModel) -> bool:
        curr_time = Clock.datetime_now()
        auth_creation_time = app_auth.created_at
        duration_since_creation = curr_time - auth_creation_time
        duration_sec = duration_since_creation.total_seconds()
        is_refresh = duration_sec > app_auth.expires_in - 300  # 5 minutes before expiry
        logger.bind(
            ctx=ctx,
            is_refresh_required=is_refresh,
            expires_in=app_auth.expires_in,
            duration_sec=duration_sec,
        ).info("HubSpot is_refresh_required")
        return is_refresh

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: OAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.access_token}"}

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: HubSpotCreds, app_auth: OAuthModel, url: str
    ) -> str:
        logger.bind(ctx=ctx, new_url=url).info("Updating url for HubSpot")
        try:
            http_url = HttpUrl(url)
            hubspot_url = "https://api.hubapi.com"
            url = url.replace(f"{http_url.scheme}://{http_url.host}", hubspot_url)
            return url
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Get updated url of HubSpot: {str(e)}")
            return url
