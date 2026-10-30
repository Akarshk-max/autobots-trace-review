import urllib.parse

# meta_auth.py
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


class MetaCreds(BaseModel):
    email: str


class MetaAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.meta

    # Meta Ads read-only is the same handshake with a different app and narrower scopes,
    # so it subclasses this and answers these three rather than repeating the URL.

    @staticmethod
    def oauth_client_id() -> str:
        return SettingsProvider.sget().META_CLIENT_ID

    @staticmethod
    def oauth_scopes() -> str:
        return SettingsProvider.sget().META_SCOPES

    @staticmethod
    def oauth_callback_path() -> str:
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}/meta/oauth-callback"

    @classmethod
    def build_authorize_url(cls, ctx: Context, name: str, callback_url: str, post_auth_uri: str) -> str:
        """The consent URL, built here so the route and connect() cannot drift.

        `callback_url` is ours and is what Meta matches against its registration;
        `post_auth_uri` is where the user lands afterwards and rides inside signed state.
        """
        state_b64 = new_oauth_state(ctx=ctx, name=name, redirect_uri=post_auth_uri).to_base64()
        return (
            f"{Meta.AUTHORIZATION_CODE_URL}?"
            f"client_id={cls.oauth_client_id()}&"
            f"redirect_uri={urllib.parse.quote_plus(callback_url)}&"
            f"response_type=code&"
            f"scope={cls.oauth_scopes()}&"
            f"state={urllib.parse.quote_plus(state_b64)}"
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
        """No creds to turn into auth — the user has to visit Meta."""
        return IntegrationConnectResult(
            redirect_url=cls.build_authorize_url(
                ctx=ctx,
                name=req.name,
                callback_url=req.callback_url or "",
                post_auth_uri=req.post_auth_uri or "",
            )
        )

    @staticmethod
    async def get_api_domain(app_creds: MetaCreds | None):
        return "https://graph.facebook.com"

    @staticmethod
    def get_creds_model_type() -> Type[MetaCreds]:
        return MetaCreds

    @staticmethod
    def get_auth_model_type() -> Type[OAuthModel]:
        return OAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: MetaAccessTokenRq) -> OAuthModel | Exception:
        token = await Meta.access_token(ctx=ctx, rq=app_creds)
        match token:
            case MetaAccessToken():
                res = OAuthModel.model_validate(token.model_dump())
                res.code = app_creds.code
                return res
            case Exception():
                return token

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: MetaCreds | None, app_auth: OAuthModel | None
    ) -> OAuthModel | Exception:
        token_rq = MetaRefreshTokenRq(
            grant_type="fb_exchange_token",
            client_id=SettingsProvider.sget().META_CLIENT_ID,
            client_secret=SettingsProvider.sget().META_CLIENT_SECRET,
            fb_exchange_token=app_auth.access_token,
        )
        refreshed = await Meta.refresh_access_token(ctx, token_rq)
        match refreshed:
            case MetaRefreshedAccessToken():
                new_auth_model = OAuthModel.model_validate(app_auth)
                new_auth_model.access_token = refreshed.access_token
                new_auth_model.expires_in = refreshed.expires_in
                new_auth_model.created_at = Clock.datetime_now()
                logger.bind(ctx=ctx).info("Refreshed Meta token")
                return new_auth_model
            case Exception():
                logger.bind(ctx=ctx).error("Unable to refresh Meta token")
                return refreshed

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: OAuthModel) -> bool:
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
        ctx: Context, app_auth: OAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.access_token}"}
