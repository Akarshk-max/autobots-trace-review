from typing import Dict, Optional, Type
from urllib import parse

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, HttpUrl

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


class GoogleCreds(BaseModel):
    email: str


class GoogleAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.google

    @staticmethod
    async def get_api_domain(app_creds: GoogleCreds | None):
        return "https://gmail.googleapis.com"

    @staticmethod
    def get_creds_model_type() -> Type[GoogleCreds]:
        return GoogleCreds

    @staticmethod
    def get_auth_model_type() -> Type[OAuthModel]:
        return OAuthModel

    # The seven Google integrations differ only in which credentials they use and where
    # their callback lives. They subclass this, so the consent URL is written once and
    # each variant answers these three.

    @staticmethod
    def oauth_client_id() -> str:
        return SettingsProvider.sget().GOOGLE_CLIENT_ID

    @staticmethod
    def oauth_scopes() -> str:
        return SettingsProvider.sget().GOOGLE_SCOPES

    @staticmethod
    def oauth_callback_path() -> str:
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}/google/oauth-callback"

    @classmethod
    def build_authorize_url(cls, ctx: Context, name: str, callback_url: str, post_auth_uri: str) -> str:
        """The consent URL, built here so the route and connect() cannot drift.

        `callback_url` is ours and is what Google matches against its registration;
        `post_auth_uri` is where the user lands afterwards and rides inside signed state.
        """
        state_b64 = new_oauth_state(ctx=ctx, name=name, redirect_uri=post_auth_uri).to_base64()
        return (
            f"{Google.AUTHORIZATION_CODE_URL}?"
            f"client_id={cls.oauth_client_id()}&"
            f"redirect_uri={parse.quote_plus(callback_url)}&"
            f"response_type=code&"
            f"scope={cls.oauth_scopes()}&"
            f"access_type=offline&"
            f"prompt=consent&"
            f"state={parse.quote_plus(state_b64)}"
        )

    @classmethod
    async def connect(
        cls,
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        req: IntegrationConnectResolved,
        decrypt: bool = False,
    ) -> IntegrationConnectResult:
        """No creds to turn into auth — the user has to visit Google."""
        return IntegrationConnectResult(
            redirect_url=cls.build_authorize_url(
                ctx=ctx,
                name=req.name,
                callback_url=req.callback_url or "",
                post_auth_uri=req.post_auth_uri or "",
            )
        )

    # @staticmethod
    # async def create_auth_from_callback(ctx: Context, app_creds: OAuthCredsWithRedirectModel, google_code: str):

    @staticmethod
    async def create_auth(ctx: Context, app_creds: GoogleAccessTokenRq) -> OAuthModel | Exception:
        access_token = await Google.access_token(ctx=ctx, rq=app_creds)
        match access_token:
            case GoogleAccessToken():
                res = OAuthModel.model_validate(access_token.model_dump())
                res.code = app_creds.code
                return res
            case Exception():
                return access_token

    @staticmethod
    async def revoke_external_auth(
        ctx: Context, app_creds: GoogleCreds | None, app_auth: OAuthModel | None
    ) -> bool | Exception:
        """Revoke the Google OAuth grant by revoking the stored token(s).

        Prefer revoking the refresh token (if present). If that fails, fall back to
        revoking the access token. Revocation is what removes the app from the
        user's Google Account "Third-party access" list (subject to propagation delays).

        Note: app_creds is currently unused for Google revoke but is accepted to
        conform to the IntegrationTypeABC.revoke_external_auth(...) signature.
        """
        _ = app_creds  # unused
        if not app_auth:
            return HTTPException(status_code=400, detail="Missing app auth for Google")
        if not app_auth.refresh_token and not app_auth.access_token:
            return HTTPException(status_code=400, detail="No token available for Google")

        # 1) Try refresh token first (best for invalidating long-lived access)
        if app_auth.refresh_token:
            revoke_rs = await Google.revoke_token(ctx=ctx, token=app_auth.refresh_token)
            if not isinstance(revoke_rs, Exception):
                logger.bind(ctx=ctx).info("Revoked Google refresh token")
                return True
            logger.bind(ctx=ctx).warning(f"Unable to revoke Google refresh token: {revoke_rs}")

        # 2) Fall back to access token
        if app_auth.access_token:
            revoke_rs = await Google.revoke_token(ctx=ctx, token=app_auth.access_token)
            if not isinstance(revoke_rs, Exception):
                logger.bind(ctx=ctx).info("Revoked Google access token")
                return True
            logger.bind(ctx=ctx).warning(f"Unable to revoke Google access token: {revoke_rs}")
            return HTTPException(status_code=400, detail=f"Unable to revoke Google access token: {revoke_rs}")

        return HTTPException(status_code=400, detail="No token available for Google")
    
    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: GoogleCreds | None, app_auth: OAuthModel | None
    ) -> OAuthModel | Exception:
        token_rq = GoogleRefreshTokenRq(
            client_id=SettingsProvider.sget().GOOGLE_CLIENT_ID,
            client_secret=SettingsProvider.sget().GOOGLE_CLIENT_SECRET,
            refresh_token=app_auth.refresh_token,
        )
        refreshed_access_token = await Google.refresh_access_token(ctx, token_rq)
        match refreshed_access_token:
            case GoogleRefreshedAccessToken():
                new_auth_model = OAuthModel.model_validate(app_auth)
                new_auth_model.access_token = refreshed_access_token.access_token
                new_auth_model.expires_in = refreshed_access_token.expires_in
                new_auth_model.created_at = Clock.datetime_now()
                logger.bind(ctx=ctx).info("Refreshed token for Google")
                return new_auth_model
            case Exception():
                logger.bind(ctx=ctx).error("Unable to refresh token for Google")
                return refreshed_access_token

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
        ctx: Context, app_creds: Optional[GoogleCreds], app_auth: OAuthModel, url: str
    ) -> str:
        if not app_creds:
            return url
        http_url = HttpUrl(url)
        path_parts = http_url.path.split("/")
        for i in range(len(path_parts)):
            if path_parts[i] == "users":
                user_email_part = i + 1
                user_email_url_encoded = parse.quote(app_creds.email)
                path_parts[user_email_part] = user_email_url_encoded
        new_path = "/".join(path_parts)
        new_url = url.replace(http_url.path, new_path)
        return new_url

    # @staticmethod
    # async def get_auth_header(
    #         ctx: Context,
    #         app_creds: OAuthCredsWithRedirectModel | None = None,
    #         app_auth: OAuthModel | None = None
    # ) -> Dict[str, str]:
    #     if app_creds is None and app_auth is None:
    #         return {}
    #     if app_creds and app_auth:
    #         refreshed_auth = await GoogleAuth.refresh_auth(ctx, app_creds, app_auth)
    #         return {"Authorization": f"Bearer {refreshed_auth.access_token}"}
    #     if app_creds:
    #         new_auth = await GoogleAuth.create_auth(ctx, app_creds)
    #         return {"Authorization": f"Bearer {new_auth.access_token}"}
