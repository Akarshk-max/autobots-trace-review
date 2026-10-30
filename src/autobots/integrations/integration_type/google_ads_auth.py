import urllib.parse

# google_ads_auth.py
from typing import Dict, Optional, Type

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots import SettingsProvider
from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.api.v1.api_auth.oauth_state import new_oauth_state
from src.autobots.conn.only_auth.google.google import (
    Google,
    GoogleAccessToken,
    GoogleAccessTokenRq,
    GoogleRefreshedAccessToken,
    GoogleRefreshTokenRq,
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


class GoogleAdsCreds(BaseModel):
    email: str
    login_customer_id: str  # User's Google Ads customer ID (no dashes)


class GoogleAdsOAuthModel(OAuthModel):
    """Extended OAuth model for Google Ads with user's login_customer_id."""

    login_customer_id: str | None = None


class GoogleAdsAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.google_ads  # add to your enum

    @staticmethod
    def oauth_callback_path() -> str:
        """Where Google sends the browser back. Registered in its console."""
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}/google-ads/oauth-callback"

    @staticmethod
    def build_authorize_url(
        ctx: Context, name: str, callback_url: str, post_auth_uri: str
    ) -> str:
        """The consent URL, built here so the route and connect() cannot drift.

        `callback_url` is ours and is what the provider matches against its registration;
        `post_auth_uri` is where the user lands afterwards and rides inside signed state.
        """
        client_id = SettingsProvider.sget().GOOGLE_ADS_CLIENT_ID
        # typically just https://www.googleapis.com/auth/adwords
        scopes = SettingsProvider.sget().GOOGLE_ADS_SCOPES
        state_b64 = new_oauth_state(ctx=ctx, name=name, redirect_uri=post_auth_uri).to_base64()
        url_encoded_callback_uri = urllib.parse.quote_plus(callback_url)
        return (
            f"{Google.AUTHORIZATION_CODE_URL}?"
            f"client_id={client_id}&"
            f"redirect_uri={url_encoded_callback_uri}&"
            f"response_type=code&"
            f"scope={scopes}&"
            f"access_type=offline&"
            f"state={urllib.parse.quote_plus(state_b64)}&"
            f"prompt=consent"
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
            redirect_url=GoogleAdsAuth.build_authorize_url(
                ctx=ctx,
                name=req.name,
                callback_url=req.callback_url or "",
                post_auth_uri=req.post_auth_uri or "",
            )
        )

    @staticmethod
    async def get_api_domain(app_creds: GoogleAdsCreds | None):
        return "https://googleads.googleapis.com"

    @staticmethod
    def get_creds_model_type() -> Type[GoogleAdsCreds]:
        return GoogleAdsCreds

    @staticmethod
    def get_auth_model_type() -> Type[GoogleAdsOAuthModel]:
        return GoogleAdsOAuthModel

    @staticmethod
    async def create_auth(
        ctx: Context, app_creds: GoogleAccessTokenRq
    ) -> GoogleAdsOAuthModel | Exception:
        access_token = await Google.access_token(ctx=ctx, rq=app_creds)
        match access_token:
            case GoogleAccessToken():
                res = GoogleAdsOAuthModel.model_validate(access_token.model_dump())
                res.code = app_creds.code
                return res
            case Exception():
                return access_token

    @staticmethod
    async def revoke_external_auth(
        ctx: Context, app_creds: GoogleAdsCreds | None, app_auth: GoogleAdsOAuthModel | None
    ) -> bool | Exception:
        """Revoke the Google OAuth grant for Google Ads by revoking token(s).

        Prefer revoking the refresh token (if present). If that fails, fall back to
        revoking the access token.

        Note: app_creds is currently unused for Google revoke but is accepted to
        conform to the IntegrationTypeABC.revoke_external_auth(...) signature.
        """
        _ = app_creds  # unused
        if not app_auth:
            return Exception("Missing app auth for Google Ads")
        if not app_auth.refresh_token and not app_auth.access_token:
            return Exception("No token available for Google Ads")

        if app_auth.refresh_token:
            revoke_rs = await Google.revoke_token(ctx=ctx, token=app_auth.refresh_token)
            if not isinstance(revoke_rs, Exception):
                logger.bind(ctx=ctx).info("Revoked Google Ads refresh token")
                return True
            logger.bind(ctx=ctx).warning(f"Unable to revoke Google Ads refresh token: {revoke_rs}")

        if app_auth.access_token:
            revoke_rs = await Google.revoke_token(ctx=ctx, token=app_auth.access_token)
            if not isinstance(revoke_rs, Exception):
                logger.bind(ctx=ctx).info("Revoked Google Ads access token")
                return True
            logger.bind(ctx=ctx).warning(f"Unable to revoke Google Ads access token: {revoke_rs}")
            return revoke_rs

        return Exception("No token available for Google Ads")

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: GoogleAdsCreds | None, app_auth: GoogleAdsOAuthModel | None
    ) -> GoogleAdsOAuthModel | Exception:
        token_rq = GoogleRefreshTokenRq(
            client_id=SettingsProvider.sget().GOOGLE_ADS_CLIENT_ID,
            client_secret=SettingsProvider.sget().GOOGLE_ADS_CLIENT_SECRET,
            refresh_token=app_auth.refresh_token,
        )
        refreshed = await Google.refresh_access_token(ctx, token_rq)
        match refreshed:
            case GoogleRefreshedAccessToken():
                new_auth = GoogleAdsOAuthModel.model_validate(app_auth)
                new_auth.access_token = refreshed.access_token
                new_auth.expires_in = refreshed.expires_in
                new_auth.created_at = Clock.datetime_now()
                # login_customer_id is preserved via model_validate(app_auth)
                logger.bind(ctx=ctx).info("Refreshed token for Google Ads")
                return new_auth
            case Exception():
                logger.bind(ctx=ctx).error("Unable to refresh token for Google Ads")
                return refreshed

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: GoogleAdsOAuthModel) -> bool:
        curr_time = Clock.datetime_now()
        auth_creation_time = app_auth.created_at
        duration_since_creation = curr_time - auth_creation_time
        duration_sec = duration_since_creation.total_seconds()
        is_refresh = duration_sec > app_auth.expires_in - 600  # refresh 10 min early
        logger.bind(
            ctx=ctx,
            is_refresh_required=is_refresh,
            expires_in=app_auth.expires_in,
            duration_sec=duration_sec,
            curr_time=str(curr_time),
            auth_creation_time=str(auth_creation_time),
        ).info("is_refresh_required")
        return is_refresh

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: GoogleAdsOAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        """
        Headers required by Google Ads REST:
          - Authorization: Bearer <access_token>
          - developer-token: <developer token>
          - login-customer-id: <user's customer ID or fallback to MCC ID>   (no dashes)
        """
        dev_token = SettingsProvider.sget().GOOGLE_ADS_DEVELOPER_TOKEN

        if app_auth.login_customer_id:
            login_customer_id = app_auth.login_customer_id
            logger.bind(ctx=ctx).info(
                f"[AUTH_HEADER] Using user's login_customer_id: {login_customer_id}"
            )
        else:
            login_customer_id = SettingsProvider.sget().GOOGLE_ADS_LOGIN_CUSTOMER_ID
            logger.bind(ctx=ctx).info(
                f"[AUTH_HEADER] Using fallback MCC login_customer_id: {login_customer_id} (from system settings)"
            )
        return {
            "Authorization": f"Bearer {app_auth.access_token}",
            "developer-token": dev_token,
            "login-customer-id": login_customer_id.replace("-", ""),
        }
