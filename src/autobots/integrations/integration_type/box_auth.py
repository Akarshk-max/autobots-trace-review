import urllib.parse
from typing import Dict, Optional, Type

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, HttpUrl

from src.autobots import SettingsProvider
from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.api.v1.api_auth.oauth_state import new_oauth_state
from src.autobots.conn.only_auth.box.box_client import (
    Box,
    BoxAccessToken,
    BoxAccessTokenRq,
    BoxRefreshedAccessToken,
    BoxRefreshTokenRq,
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


class BoxCreds(BaseModel):
    api_url: str = "https://api.box.com"


class BoxAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.box

    @staticmethod
    def oauth_callback_path() -> str:
        """Where the provider sends the browser back. Registered in its console."""
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}/box/oauth-callback"

    @staticmethod
    def build_authorize_url(
        ctx: Context, name: str, callback_url: str, post_auth_uri: str
    ) -> str:
        """The consent URL, built here so the route and connect() cannot drift.

        `callback_url` is ours and is what the provider matches against its registration;
        `post_auth_uri` is where the user lands afterwards and rides inside signed state.
        """
        client_id = SettingsProvider.sget().BOX_CLIENT_ID
        state_b64 = new_oauth_state(ctx=ctx, name=name, redirect_uri=post_auth_uri).to_base64()
        url_encoded_callback_uri = urllib.parse.quote_plus(callback_url)
        oauth_url = (
            "https://account.box.com/api/oauth2/authorize?"
            f"client_id={client_id}&"
            f"redirect_uri={url_encoded_callback_uri}&"
            "response_type=code&"
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
            redirect_url=BoxAuth.build_authorize_url(
                ctx=ctx,
                name=req.name,
                callback_url=req.callback_url or "",
                post_auth_uri=req.post_auth_uri or "",
            )
        )

    @staticmethod
    async def get_api_domain(app_creds: BoxCreds | None):
        return app_creds.api_url if app_creds else "https://api.box.com"

    @staticmethod
    def get_creds_model_type() -> Type[BoxCreds]:
        return BoxCreds

    @staticmethod
    def get_auth_model_type() -> Type[OAuthModel]:
        return OAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: BoxAccessTokenRq) -> OAuthModel | Exception:
        access_token = await Box.access_token(ctx=ctx, rq=app_creds)
        match access_token:
            case BoxAccessToken():
                res = OAuthModel.model_validate(access_token.model_dump())
                res.code = app_creds.code
                return res
            case Exception():
                return access_token

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: BoxCreds | None, app_auth: OAuthModel | None
    ) -> OAuthModel | Exception:
        token_rq = BoxRefreshTokenRq(
            client_id=SettingsProvider.sget().BOX_CLIENT_ID,
            client_secret=SettingsProvider.sget().BOX_CLIENT_SECRET,
            refresh_token=app_auth.refresh_token,
        )
        refreshed_access_token = await Box.refresh_access_token(ctx, token_rq)
        match refreshed_access_token:
            case BoxRefreshedAccessToken():
                new_auth_model = OAuthModel.model_validate(app_auth)
                new_auth_model.access_token = refreshed_access_token.access_token
                new_auth_model.refresh_token = refreshed_access_token.refresh_token
                new_auth_model.expires_in = refreshed_access_token.expires_in
                new_auth_model.created_at = Clock.datetime_now()
                logger.bind(ctx=ctx).info("Refreshed token for Box")
                return new_auth_model
            case Exception() as error:
                logger.bind(ctx=ctx).exception(f"Unable to refresh token for Box: {str(error)}")
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
        ctx: Context, app_creds: BoxCreds, app_auth: OAuthModel, url: str
    ) -> str:
        logger.bind(ctx=ctx, new_url=url).info("Updating url")
        try:
            http_url = HttpUrl(url)
            box_url = await BoxAuth.get_api_domain(app_creds)
            url = url.replace(f"{http_url.scheme}://{http_url.host}", box_url)
            return url
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Get updated url of Box: {str(e)}")
