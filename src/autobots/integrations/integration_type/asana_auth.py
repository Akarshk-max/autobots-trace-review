import urllib.parse
from typing import Dict, Optional, Type

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, HttpUrl

from src.autobots import SettingsProvider
from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.api.v1.api_auth.oauth_state import new_oauth_state
from src.autobots.conn.only_auth.asana.asana_client import (
    Asana,
    AsanaAccessTokenRq,
    AsanaRefreshTokenRq,
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


class AsanaCreds(BaseModel):
    pass


class AsanaAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.asana

    @staticmethod
    def oauth_callback_path() -> str:
        """Where the provider sends the browser back. Registered in its console."""
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}/asana/oauth-callback"

    @staticmethod
    def build_authorize_url(
        ctx: Context, name: str, callback_url: str, post_auth_uri: str
    ) -> str:
        """The consent URL, built here so the route and connect() cannot drift.

        `callback_url` is ours and is what the provider matches against its registration;
        `post_auth_uri` is where the user lands afterwards and rides inside signed state.
        """
        client_id = SettingsProvider.sget().ASANA_CLIENT_ID
        scopes = SettingsProvider.sget().ASANA_SCOPES
        state_b64 = new_oauth_state(ctx=ctx, name=name, redirect_uri=post_auth_uri).to_base64()
        url_encoded_callback_uri = urllib.parse.quote_plus(callback_url)
        oauth_url = (
            "https://app.asana.com/-/oauth_authorize?"
            f"client_id={client_id}&"
            f"redirect_uri={url_encoded_callback_uri}&"
            f"response_type=code&"
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
            redirect_url=AsanaAuth.build_authorize_url(
                ctx=ctx,
                name=req.name,
                callback_url=req.callback_url or "",
                post_auth_uri=req.post_auth_uri or "",
            )
        )

    @staticmethod
    async def get_api_domain(app_creds: AsanaCreds | None):
        return "https://app.asana.com/api/1.0"

    @staticmethod
    def get_creds_model_type() -> Type[AsanaCreds]:
        return AsanaCreds

    @staticmethod
    def get_auth_model_type() -> Type[OAuthModel]:
        return OAuthModel

    @staticmethod
    async def create_auth(
        ctx: Context,
        app_creds: AsanaAccessTokenRq,
    ) -> OAuthModel | Exception:
        try:
            access_token = await Asana.access_token(ctx=ctx, rq=app_creds)
            if isinstance(access_token, Exception):
                return access_token

            return OAuthModel(
                access_token=access_token.access_token,
                refresh_token=access_token.refresh_token,
                expires_in=access_token.expires_in,
                token_type=access_token.token_type,
            )
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error creating Asana auth: {e}")
            return e

    @staticmethod
    async def refresh_auth(
        ctx: Context,
        app_creds: AsanaCreds,
        app_auth: OAuthModel,
    ) -> OAuthModel | Exception:
        """
        Refresh an expiring access token.
        """
        try:
            refresh_rq = AsanaRefreshTokenRq(refresh_token=app_auth.refresh_token)
            refreshed_token = await Asana.refresh_access_token(ctx=ctx, rq=refresh_rq)
            if isinstance(refreshed_token, Exception):
                return refreshed_token

            return OAuthModel(
                access_token=refreshed_token.access_token,
                refresh_token=app_auth.refresh_token,  # keep existing RT
                expires_in=refreshed_token.expires_in,
                token_type=refreshed_token.token_type,
            )
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error refreshing Asana auth: {e}")
            return e

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: OAuthModel) -> bool:
        now = Clock.datetime_now()
        age_sec = (now - app_auth.created_at).total_seconds()
        needs_refresh = age_sec > app_auth.expires_in - 300
        logger.bind(
            ctx=ctx,
            is_refresh_required=needs_refresh,
            expires_in=app_auth.expires_in,
            age_sec=age_sec,
        ).info("Asana is_refresh_required")
        return needs_refresh

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: OAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.access_token}"}

    @staticmethod
    async def get_updated_url(
        ctx: Context,
        app_creds: AsanaCreds,
        app_auth: OAuthModel,
        url: str,
    ) -> str:
        logger.bind(ctx=ctx, old_url=url).info("Updating URL for Asana")
        try:
            http_url = HttpUrl(url)
            base = await AsanaAuth.get_api_domain(app_creds)

            # Check if URL already has the API path
            if "/api/1.0" in http_url.path:
                # URL already has the full path, just ensure the host is correct
                return url.replace(f"{http_url.scheme}://{http_url.host}", "https://app.asana.com")
            else:
                # Replace host with full API base URL
                return url.replace(f"{http_url.scheme}://{http_url.host}", base)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"get_updated_url failed: {e}")
            return url
