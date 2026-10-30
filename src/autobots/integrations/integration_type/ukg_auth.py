import urllib.parse
from typing import Dict, Optional, Type

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, HttpUrl

from src.autobots import SettingsProvider
from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.api.v1.api_auth.oauth_state import new_oauth_state
from src.autobots.conn.only_auth.ukg.ukg_client import (
    UKG,
    UKGAccessToken,
    UKGAccessTokenRq,
    UKGClientCredentialsRq,
    UKGClientCredentialsToken,
    UKGRefreshedAccessToken,
    UKGRefreshTokenRq,
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


class UKGCreds(BaseModel):
    client_id: str
    client_secret: str
    base_url: str  # Customer-specific UKG URL (e.g., https://customer.ultipro.com)


class UKGAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.ukg

    @staticmethod
    def oauth_callback_path() -> str:
        """Where the provider sends the browser back. Registered in its console."""
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_PATH_UKG}/oauth-callback"

    @staticmethod
    def build_authorize_url(
        ctx: Context,
        name: str,
        callback_url: str,
        post_auth_uri: str,
        app_creds: "UKGCreds | None" = None,
    ) -> str:
        """The consent URL, built here so the route and connect() cannot drift.

        This provider's authorize host comes from the customer's own credentials, so
        unlike the settings-only providers it cannot build a URL without them.
        """
        if not app_creds or not app_creds.base_url:
            raise HTTPException(
                400, "UKG base_url is required. Please configure your customer-specific UKG URL."
            )
        if not app_creds.client_id or not app_creds.client_secret:
            raise HTTPException(400, "UKG client_id and client_secret are required.")
        url_encoded_callback_uri = urllib.parse.quote_plus(callback_url)
        url_encoded_scope = urllib.parse.quote_plus(SettingsProvider.sget().UKG_SCOPES)
        state_b64 = new_oauth_state(ctx=ctx, name=name, redirect_uri=post_auth_uri).to_base64()
        return (
            f"{UKG.get_auth_url(app_creds.base_url)}?"
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
            UKGCreds.model_validate(creds_doc.creds) if creds_doc and creds_doc.creds else None
        )
        return IntegrationConnectResult(
            redirect_url=UKGAuth.build_authorize_url(
                ctx=ctx,
                name=req.name,
                callback_url=req.callback_url or "",
                post_auth_uri=req.post_auth_uri or "",
                app_creds=app_creds,
            )
        )

    @staticmethod
    async def get_api_domain(app_creds: UKGCreds | None):
        return app_creds.base_url if app_creds else None

    @staticmethod
    def get_creds_model_type() -> Type[UKGCreds]:
        return UKGCreds

    @staticmethod
    def get_auth_model_type() -> Type[OAuthModel]:
        return OAuthModel

    @staticmethod
    async def create_auth(
        ctx: Context, app_creds: UKGCreds | UKGAccessTokenRq
    ) -> OAuthModel | Exception:
        """
        Create OAuth model using UKG credentials.

        Supports two flows:
        1. Client Credentials Flow (recommended, simpler) - pass UKGCreds
        2. Authorization Code Flow (if needed) - pass UKGAccessTokenRq
        """
        # Client Credentials Flow (simpler, no user authorization required)
        if isinstance(app_creds, UKGCreds):
            scope = (SettingsProvider.sget().UKG_SCOPES or "").strip() or None
            token_rq = UKGClientCredentialsRq(
                client_id=app_creds.client_id,
                client_secret=app_creds.client_secret,
                base_url=app_creds.base_url,
                scope=scope,
            )
            access_token = await UKG.client_credentials_token(ctx=ctx, rq=token_rq)
            match access_token:
                case UKGClientCredentialsToken():
                    res = OAuthModel(
                        access_token=access_token.access_token,
                        token_type=access_token.token_type,
                        expires_in=access_token.expires_in,
                        created_at=access_token.created_at,
                        scope=access_token.scope,
                        refresh_token=None,  # Client credentials flow doesn't use refresh tokens
                        code=None,
                    )
                    return res
                case Exception():
                    return access_token

        # Authorization Code Flow (requires user authorization)
        elif isinstance(app_creds, UKGAccessTokenRq):
            access_token = await UKG.access_token(ctx=ctx, rq=app_creds)
            match access_token:
                case UKGAccessToken():
                    res = OAuthModel.model_validate(access_token.model_dump())
                    res.code = app_creds.code
                    return res
                case Exception():
                    return access_token

        else:
            return Exception(f"Invalid app_creds type: {type(app_creds)}")

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: UKGCreds | None, app_auth: OAuthModel | None
    ) -> OAuthModel | Exception:
        """
        Refresh auth token.

        For client credentials flow: gets a new token (no refresh token)
        For authorization code flow: uses refresh token if available
        """
        if not app_creds:
            return Exception("UKG credentials are required for token refresh")

        # If no refresh_token, use client credentials flow (get new token)
        if not app_auth or not app_auth.refresh_token:
            logger.bind(ctx=ctx).info("Refreshing UKG token using client credentials flow")
            return await UKGAuth.create_auth(ctx, app_creds)

        # If refresh_token exists, use it (authorization code flow)
        token_rq = UKGRefreshTokenRq(
            client_id=app_creds.client_id,
            client_secret=app_creds.client_secret,
            refresh_token=app_auth.refresh_token,
            base_url=app_creds.base_url,
        )

        refreshed_access_token = await UKG.refresh_access_token(ctx, token_rq)
        match refreshed_access_token:
            case UKGRefreshedAccessToken():
                new_auth_model = OAuthModel.model_validate(app_auth)
                new_auth_model.access_token = refreshed_access_token.access_token
                new_auth_model.expires_in = refreshed_access_token.expires_in
                new_auth_model.created_at = Clock.datetime_now()
                logger.bind(ctx=ctx).info("Refreshed token for UKG using refresh token")
                return new_auth_model
            case Exception():
                logger.bind(ctx=ctx).error(
                    "Unable to refresh token for UKG, trying client credentials"
                )
                # Fallback to client credentials flow
                return await UKGAuth.create_auth(ctx, app_creds)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: OAuthModel) -> bool:
        curr_time = Clock.datetime_now()
        auth_creation_time = app_auth.created_at
        duration_since_creation = curr_time - auth_creation_time
        duration_sec = duration_since_creation.total_seconds()
        is_refresh = duration_sec > app_auth.expires_in - 600
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
        ctx: Context, app_auth: OAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.access_token}"}

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: UKGCreds, app_auth: OAuthModel, url: str
    ) -> str:
        logger.bind(ctx=ctx, new_url=url).info("Updating url")
        try:
            if not url.startswith("http"):
                url = f"https://{url}"
            http_url = HttpUrl(url)

            ukg_url = await UKGAuth.get_api_domain(app_creds)
            url = url.replace(f"{http_url.scheme}://{http_url.host}", ukg_url)
            return url
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Get updated url of UKG: {str(e)}")
            return url
