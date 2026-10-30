import urllib.parse
from typing import Dict, Optional, Type

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots import SettingsProvider
from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.api.v1.api_auth.oauth_state import new_oauth_state
from src.autobots.conn.only_auth.zoom.zoom import (
    Zoom,
    ZoomAccessToken,
    ZoomAccessTokenRq,
    ZoomRefreshedAccessToken,
    ZoomRefreshTokenRq,
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


class ZoomCreds(BaseModel):
    email: str


class ZoomAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.zoom

    @staticmethod
    def oauth_callback_path() -> str:
        """Where Zoom sends the browser back. Registered in its console."""
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_PATH_ZOOM}/oauth-callback"

    @staticmethod
    def build_authorize_url(
        ctx: Context, name: str, callback_url: str, post_auth_uri: str
    ) -> str:
        """The consent URL, built here so the route and connect() cannot drift.

        `callback_url` is ours and is what the provider matches against its registration;
        `post_auth_uri` is where the user lands afterwards and rides inside signed state.
        """
        client_id = SettingsProvider.sget().ZOOM_CLIENT_ID
        state_b64 = new_oauth_state(ctx=ctx, name=name, redirect_uri=post_auth_uri).to_base64()
        url_encoded_callback_uri = urllib.parse.quote_plus(callback_url)
        url_encoded_scope = urllib.parse.quote_plus(SettingsProvider.sget().ZOOM_SCOPES)
        return (
            f"{Zoom.AUTHORIZATION_CODE_URL}?"
            f"response_type=code&"
            f"client_id={client_id}&"
            f"redirect_uri={url_encoded_callback_uri}&"
            f"scope={url_encoded_scope}&"
            f"state={urllib.parse.quote_plus(state_b64)}"
        )

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
            redirect_url=ZoomAuth.build_authorize_url(
                ctx=ctx,
                name=req.name,
                callback_url=req.callback_url or "",
                post_auth_uri=req.post_auth_uri or "",
            )
        )

    @staticmethod
    async def get_api_domain(app_creds: ZoomCreds | None) -> str:
        return "https://api.zoom.us"

    @staticmethod
    def get_creds_model_type() -> Type[ZoomCreds]:
        return ZoomCreds

    @staticmethod
    def get_auth_model_type() -> Type[OAuthModel]:
        return OAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: ZoomAccessTokenRq) -> OAuthModel | Exception:
        access_token = await Zoom.access_token(ctx=ctx, rq=app_creds)
        match access_token:
            case ZoomAccessToken():
                res = OAuthModel.model_validate(access_token.model_dump())
                res.code = app_creds.code
                return res
            case Exception():
                return access_token

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: ZoomCreds | None, app_auth: OAuthModel | None
    ) -> OAuthModel | Exception:
        if not app_auth or not app_auth.refresh_token:
            return Exception("No refresh token available for Zoom")

        token_rq = ZoomRefreshTokenRq(
            client_id=SettingsProvider.sget().ZOOM_CLIENT_ID,
            client_secret=SettingsProvider.sget().ZOOM_CLIENT_SECRET,
            refresh_token=app_auth.refresh_token,
        )
        refreshed_access_token = await Zoom.refresh_access_token(ctx, token_rq)
        match refreshed_access_token:
            case ZoomRefreshedAccessToken():
                new_auth_model = OAuthModel.model_validate(app_auth)
                new_auth_model.access_token = refreshed_access_token.access_token
                new_auth_model.expires_in = refreshed_access_token.expires_in
                new_auth_model.created_at = Clock.datetime_now()
                logger.bind(ctx=ctx).info("Refreshed token for Zoom")
                return new_auth_model
            case Exception():
                logger.bind(ctx=ctx).error("Unable to refresh token for Zoom")
                return refreshed_access_token

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: OAuthModel) -> bool:
        # Zoom access tokens expire after 1 hour (3600 seconds)
        # Refresh if we have less than 10 minutes (600 seconds) remaining
        if not app_auth.created_at:
            return False

        curr_time = Clock.datetime_now()
        auth_creation_time = app_auth.created_at
        duration_since_creation = curr_time - auth_creation_time
        duration_sec = duration_since_creation.total_seconds()

        # Zoom tokens expire after 1 hour
        expires_in = app_auth.expires_in if app_auth.expires_in else 3600
        is_refresh = duration_sec > expires_in - 600

        logger.bind(
            ctx=ctx,
            is_refresh_required=is_refresh,
            expires_in=expires_in,
            duration_sec=duration_sec,
            curr_time=str(curr_time),
            auth_creation_time=str(auth_creation_time),
        ).info("is_refresh_required for Zoom")

        return is_refresh

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: OAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.access_token}"}

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: ZoomCreds, app_auth: OAuthModel, url: str
    ) -> str:
        try:
            from pydantic import HttpUrl

            http_url = HttpUrl(url)
            path_parts = http_url.path.split("/")
            for i in range(len(path_parts)):
                if path_parts[i] == "users":
                    user_email_part = i + 1
                    user_email_url_encoded = urllib.parse.quote(app_creds.email)
                    path_parts[user_email_part] = user_email_url_encoded
            new_path = "/".join(path_parts)
            new_url = url.replace(http_url.path, new_path)
            return new_url
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in Zoom get_updated_url: {str(e)}")
            return url
