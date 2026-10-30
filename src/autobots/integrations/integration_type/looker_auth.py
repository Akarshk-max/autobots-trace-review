from typing import Dict, Optional, Type

from loguru import logger
from pydantic import BaseModel, HttpUrl

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.conn.only_auth.looker.looker_client import (
    Looker,
    LookerAccessToken,
    LookerAccessTokenRq,
)
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import OAuthModel
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class LookerCreds(BaseModel):
    base_url: str  # e.g., "https://cmgroup.cloud.looker.com"
    client_id: str
    client_secret: str


class LookerAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.looker

    @staticmethod
    async def get_api_domain(app_creds: LookerCreds | None):
        return app_creds.base_url if app_creds else None

    @staticmethod
    def get_creds_model_type() -> Type[LookerCreds]:
        return LookerCreds

    @staticmethod
    def get_auth_model_type() -> Type[OAuthModel]:
        return OAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: LookerCreds) -> OAuthModel | Exception:
        """Create OAuth model using Looker client credentials flow"""
        token_rq = LookerAccessTokenRq(
            client_id=app_creds.client_id,
            client_secret=app_creds.client_secret,
            base_url=app_creds.base_url,
        )

        access_token = await Looker.access_token(ctx=ctx, rq=token_rq)
        match access_token:
            case LookerAccessToken():
                # Convert to OAuthModel format
                res = OAuthModel(
                    access_token=access_token.access_token,
                    token_type=access_token.token_type,
                    expires_in=access_token.expires_in,
                    created_at=access_token.created_at,
                    refresh_token=access_token.refresh_token,  # May be None for client credentials
                    code=None,  # Not applicable for client credentials flow
                )
                return res
            case Exception():
                return access_token

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: LookerCreds | None, app_auth: OAuthModel | None
    ) -> OAuthModel | Exception:
        """
        Refresh auth by getting a new token.
        Looker doesn't use refresh tokens, so we get a new access token.
        """
        if not app_creds:
            return Exception("Looker credentials are required for token refresh")

        logger.bind(ctx=ctx).info("Refreshing Looker token by getting new access token")

        # Get a new access token using client credentials
        return await LookerAuth.create_auth(ctx, app_creds)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: OAuthModel) -> bool:
        """Check if token refresh is required (with 5 minute buffer)"""
        curr_time = Clock.datetime_now()
        auth_creation_time = app_auth.created_at
        duration_since_creation = curr_time - auth_creation_time
        duration_sec = duration_since_creation.total_seconds()
        # Refresh 5 minutes before expiry (300 seconds)
        is_refresh = duration_sec > app_auth.expires_in - 300

        logger.bind(
            ctx=ctx,
            is_refresh_required=is_refresh,
            expires_in=app_auth.expires_in,
            duration_sec=duration_sec,
            curr_time=str(curr_time),
            auth_creation_time=str(auth_creation_time),
        ).info("is_refresh_required for Looker")

        return is_refresh

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: OAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        """Create authorization header with Bearer token"""
        return {"Authorization": f"Bearer {app_auth.access_token}"}

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: LookerCreds, app_auth: OAuthModel, url: str
    ) -> str:
        """Update URL to use the correct Looker base URL"""
        logger.bind(ctx=ctx, original_url=url).info("Updating Looker URL")
        try:
            http_url = HttpUrl(url)
            looker_base_url = await LookerAuth.get_api_domain(app_creds)

            if looker_base_url:
                # Replace the host with the Looker base URL
                updated_url = url.replace(
                    f"{http_url.scheme}://{http_url.host}", looker_base_url.rstrip("/")
                )
                logger.bind(ctx=ctx, updated_url=updated_url).info("Updated Looker URL")
                return updated_url
            else:
                logger.bind(ctx=ctx).warning("No Looker base URL found in credentials")
                return url

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error updating Looker URL: {str(e)}")
            return url
