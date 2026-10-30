import urllib.parse
from typing import Dict, Optional, Type

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, HttpUrl

from src.autobots import SettingsProvider
from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.api.v1.api_auth.oauth_state import new_oauth_state
from src.autobots.conn.only_auth.icims.icims_client import (
    ICIMS,
    ICIMSAccessToken,
    ICIMSAccessTokenRq,
    ICIMSClientCredentialsRq,
    ICIMSClientCredentialsToken,
    ICIMSRefreshedAccessToken,
    ICIMSRefreshTokenRq,
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


class ICIMSCreds(BaseModel):
    client_id: str
    client_secret: str
    customer_site: str  # e.g., "customer" or "https://customer.icims.com"


class ICIMSAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.icims

    @staticmethod
    def oauth_callback_path() -> str:
        """Where the provider sends the browser back. Registered in its console."""
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_PATH_ICIMS}/oauth-callback"

    @staticmethod
    def build_authorize_url(
        ctx: Context,
        name: str,
        callback_url: str,
        post_auth_uri: str,
        app_creds: "ICIMSCreds | None" = None,
    ) -> str:
        """The consent URL, built here so the route and connect() cannot drift.

        This provider's authorize host comes from the customer's own credentials, so
        unlike the settings-only providers it cannot build a URL without them.
        """
        if not app_creds or not app_creds.customer_site:
            raise HTTPException(
                400,
                "iCIMS customer_site is required. Please configure your customer-specific iCIMS site.",
            )
        if not app_creds.client_id or not app_creds.client_secret:
            raise HTTPException(400, "iCIMS client_id and client_secret are required.")
        url_encoded_callback_uri = urllib.parse.quote_plus(callback_url)
        scope = SettingsProvider.sget().ICIMS_SCOPES or "openid profile email"
        url_encoded_scope = urllib.parse.quote_plus(scope)
        state_b64 = new_oauth_state(ctx=ctx, name=name, redirect_uri=post_auth_uri).to_base64()
        return (
            f"{ICIMS.get_auth_url(app_creds.customer_site)}?"
            f"response_type=code&"
            f"client_id={app_creds.client_id}&"
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
        """The consent URL needs this connection's saved credentials, so load them first."""
        from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds

        creds_doc = await UserAppCreds.get_app_creds_by_name(
            ctx=ctx, db=db, user=user, name=req.name, app_name=req.app_name, decrypt=True
        )
        app_creds = (
            ICIMSCreds.model_validate(creds_doc.creds) if creds_doc and creds_doc.creds else None
        )
        return IntegrationConnectResult(
            redirect_url=ICIMSAuth.build_authorize_url(
                ctx=ctx,
                name=req.name,
                callback_url=req.callback_url or "",
                post_auth_uri=req.post_auth_uri or "",
                app_creds=app_creds,
            )
        )

    @staticmethod
    async def get_api_domain(app_creds: ICIMSCreds | None):
        if not app_creds:
            return None
        site = app_creds.customer_site
        if not site.startswith("http"):
            site = f"https://{site}.icims.com"
        return site

    @staticmethod
    def get_creds_model_type() -> Type[ICIMSCreds]:
        return ICIMSCreds

    @staticmethod
    def get_auth_model_type() -> Type[OAuthModel]:
        return OAuthModel

    @staticmethod
    async def create_auth(
        ctx: Context, app_creds: ICIMSCreds | ICIMSAccessTokenRq
    ) -> OAuthModel | Exception:
        """
        Create OAuth model using iCIMS credentials.

        Supports two flows:
        1. Client Credentials Flow - pass ICIMSCreds
        2. Authorization Code Flow - pass ICIMSAccessTokenRq
        """
        if isinstance(app_creds, ICIMSCreds):
            token_rq = ICIMSClientCredentialsRq(
                client_id=app_creds.client_id,
                client_secret=app_creds.client_secret,
                customer_site=app_creds.customer_site,
            )
            access_token = await ICIMS.client_credentials_token(ctx=ctx, rq=token_rq)
            match access_token:
                case ICIMSClientCredentialsToken():
                    res = OAuthModel(
                        access_token=access_token.access_token,
                        token_type=access_token.token_type,
                        expires_in=access_token.expires_in,
                        created_at=access_token.created_at,
                        scope=access_token.scope,
                        refresh_token=None,
                        code=None,
                    )
                    return res
                case Exception():
                    return access_token

        elif isinstance(app_creds, ICIMSAccessTokenRq):
            access_token = await ICIMS.access_token(ctx=ctx, rq=app_creds)
            match access_token:
                case ICIMSAccessToken():
                    res = OAuthModel.model_validate(access_token.model_dump())
                    res.code = app_creds.code
                    res.created_at = Clock.datetime_now()
                    return res
                case Exception():
                    return access_token

        else:
            return Exception(f"Invalid app_creds type: {type(app_creds)}")

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: ICIMSCreds | None, app_auth: OAuthModel | None
    ) -> OAuthModel | Exception:
        """
        Refresh auth token.
        """
        if not app_creds:
            return Exception("iCIMS credentials are required for token refresh")

        # If no refresh_token, use client credentials flow
        if not app_auth or not app_auth.refresh_token:
            logger.bind(ctx=ctx).info("Refreshing iCIMS token using client credentials flow")
            return await ICIMSAuth.create_auth(ctx, app_creds)

        # If refresh_token exists, use it
        token_rq = ICIMSRefreshTokenRq(
            client_id=app_creds.client_id,
            client_secret=app_creds.client_secret,
            refresh_token=app_auth.refresh_token,
            customer_site=app_creds.customer_site,
        )

        refreshed_access_token = await ICIMS.refresh_access_token(ctx, token_rq)
        match refreshed_access_token:
            case ICIMSRefreshedAccessToken():
                new_auth_model = OAuthModel.model_validate(app_auth)
                new_auth_model.access_token = refreshed_access_token.access_token
                new_auth_model.expires_in = refreshed_access_token.expires_in
                new_auth_model.created_at = Clock.datetime_now()
                logger.bind(ctx=ctx).info("Refreshed token for iCIMS using refresh token")
                return new_auth_model
            case Exception():
                logger.bind(ctx=ctx).error(
                    "Unable to refresh token for iCIMS, trying client credentials"
                )
                return await ICIMSAuth.create_auth(ctx, app_creds)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: OAuthModel) -> bool:
        curr_time = Clock.datetime_now()
        auth_creation_time = app_auth.created_at
        duration_since_creation = curr_time - auth_creation_time
        duration_sec = duration_since_creation.total_seconds()
        is_refresh = duration_sec > app_auth.expires_in - 600
        return is_refresh

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: OAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.access_token}"}

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: ICIMSCreds, app_auth: OAuthModel, url: str
    ) -> str:
        try:
            if not url.startswith("http"):
                url = f"https://{url}"
            http_url = HttpUrl(url)

            icims_url = await ICIMSAuth.get_api_domain(app_creds)
            url = url.replace(f"{http_url.scheme}://{http_url.host}", icims_url)
            return url
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Get updated url of iCIMS: {str(e)}")
            return url
