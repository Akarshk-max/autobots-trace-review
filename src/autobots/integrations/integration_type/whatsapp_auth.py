import urllib.parse
from typing import Dict, Optional, Type

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots import SettingsProvider
from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.api.v1.api_auth.oauth_state import new_oauth_state
from src.autobots.conn.only_auth.meta.meta import (
    Meta,
    MetaAccessToken,
    MetaAccessTokenRq,
    MetaRefreshedAccessToken,
    MetaRefreshTokenRq,
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


class WhatsAppCreds(BaseModel):
    phone_number_id: str
    whatsapp_business_account_id: str


class WhatsAppAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.whatsapp

    @staticmethod
    def oauth_callback_path() -> str:
        """Where Meta sends the browser back. Registered in its console."""
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_PATH_WHATSAPP}/oauth-callback"

    @staticmethod
    def build_authorize_url(
        ctx: Context, name: str, callback_url: str, post_auth_uri: str
    ) -> str:
        """The consent URL, built here so the route and connect() cannot drift.

        `callback_url` is ours and is what the provider matches against its registration;
        `post_auth_uri` is where the user lands afterwards and rides inside signed state.
        """
        client_id = SettingsProvider.sget().META_CLIENT_ID
        state_b64 = new_oauth_state(ctx=ctx, name=name, redirect_uri=post_auth_uri).to_base64()
        url_encoded_callback_uri = urllib.parse.quote_plus(callback_url)
        url_encoded_scope = urllib.parse.quote_plus(SettingsProvider.sget().WHATSAPP_SCOPES)
        return (
            f"{Meta.AUTHORIZATION_CODE_URL}?"
            f"client_id={client_id}&"
            f"redirect_uri={url_encoded_callback_uri}&"
            f"scope={url_encoded_scope}&"
            f"response_type=code&"
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
            redirect_url=WhatsAppAuth.build_authorize_url(
                ctx=ctx,
                name=req.name,
                callback_url=req.callback_url or "",
                post_auth_uri=req.post_auth_uri or "",
            )
        )

    @staticmethod
    async def get_api_domain(app_creds: WhatsAppCreds | None) -> str:
        return "https://graph.facebook.com/v18.0"

    @staticmethod
    def get_creds_model_type() -> Type[WhatsAppCreds]:
        return WhatsAppCreds

    @staticmethod
    def get_auth_model_type() -> Type[OAuthModel]:
        return OAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: MetaAccessTokenRq) -> OAuthModel | Exception:
        access_token = await Meta.access_token(ctx=ctx, rq=app_creds)
        match access_token:
            case MetaAccessToken():
                res = OAuthModel.model_validate(access_token.model_dump())
                res.code = app_creds.code
                return res
            case Exception():
                return access_token

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: WhatsAppCreds | None, app_auth: OAuthModel | None
    ) -> OAuthModel | Exception:
        if not app_auth or not app_auth.access_token:
            return Exception("No access token available for WhatsApp")

        token_rq = MetaRefreshTokenRq(
            client_id=SettingsProvider.sget().META_CLIENT_ID,
            client_secret=SettingsProvider.sget().META_CLIENT_SECRET,
            fb_exchange_token=app_auth.access_token,
        )
        refreshed_access_token = await Meta.refresh_access_token(ctx, token_rq)
        match refreshed_access_token:
            case MetaRefreshedAccessToken():
                new_auth_model = OAuthModel.model_validate(app_auth)
                new_auth_model.access_token = refreshed_access_token.access_token
                new_auth_model.expires_in = refreshed_access_token.expires_in
                new_auth_model.created_at = Clock.datetime_now()
                logger.bind(ctx=ctx).info("Refreshed WhatsApp token")
                return new_auth_model
            case Exception():
                logger.bind(ctx=ctx).error("Unable to refresh WhatsApp token")
                return refreshed_access_token

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: OAuthModel) -> bool:
        # WhatsApp tokens typically last longer than Facebook tokens
        # Facebook user tokens expire in 60 days, but WhatsApp might have different policies
        # Let's check if it's been more than 50 days (about 2 days before expiration)
        if not app_auth.created_at:
            return False

        curr_time = Clock.datetime_now()
        auth_creation_time = app_auth.created_at
        duration_since_creation = curr_time - auth_creation_time
        duration_sec = duration_since_creation.total_seconds()

        # Facebook tokens expire in about 60 days (5184000 seconds)
        # Refresh if we have less than 2 days remaining
        expires_in = app_auth.expires_in if app_auth.expires_in else 5184000  # 60 days default
        is_refresh = duration_sec > expires_in - 172800  # 2 days in seconds

        logger.bind(
            ctx=ctx,
            is_refresh_required=is_refresh,
            expires_in=expires_in,
            duration_sec=duration_sec,
            curr_time=str(curr_time),
            auth_creation_time=str(auth_creation_time),
        ).info("is_refresh_required for WhatsApp")

        return is_refresh

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: OAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.access_token}"}

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: WhatsAppCreds, app_auth: OAuthModel, url: str
    ) -> str:
        try:
            if "{phone_number_id}" in url:
                url = url.replace("{phone_number_id}", app_creds.phone_number_id)
            if "{whatsapp_business_account_id}" in url:
                url = url.replace(
                    "{whatsapp_business_account_id}", app_creds.whatsapp_business_account_id
                )
            return url
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in WhatsApp get_updated_url: {str(e)}")
            return url


class WhatsAppBusinessAuth(WhatsAppAuth):
    """
    WhatsApp Business API specific auth that may need additional business account setup.
    This inherits from WhatsAppAuth but can be customized for business-specific requirements.
    """

    @staticmethod
    async def get_api_domain(app_creds: WhatsAppCreds | None) -> str:
        return "https://graph.facebook.com/v18.0"

    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.whatsapp
