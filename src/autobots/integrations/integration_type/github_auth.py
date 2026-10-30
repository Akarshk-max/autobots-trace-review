import urllib.parse
from typing import Dict, Optional, Type

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, HttpUrl

from src.autobots import SettingsProvider
from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.api.v1.api_auth.oauth_state import new_oauth_state
from src.autobots.conn.only_auth.github.github_client import GitHub, GitHubAccessTokenRq
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import (
    IntegrationConnectResolved,
    IntegrationConnectResult,
    OAuthModel,
)
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC
from src.autobots.user.user_orm_model import UserORM


class GitHubCreds(BaseModel):
    pass


class GitHubAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.github

    @staticmethod
    async def get_api_domain(app_creds: GitHubCreds | None):
        return "https://api.github.com"

    @staticmethod
    def get_creds_model_type() -> Type[GitHubCreds]:
        return GitHubCreds

    @staticmethod
    def get_auth_model_type() -> Type[OAuthModel]:
        return OAuthModel

    @staticmethod
    def oauth_callback_path() -> str:
        """Where GitHub sends the browser back. Registered on the GitHub OAuth app."""
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}/github/oauth-callback"

    @staticmethod
    def build_authorize_url(ctx: Context, name: str, callback_url: str, post_auth_uri: str) -> str:
        """The consent URL, built in one place so the route and connect() cannot drift.

        `callback_url` is ours and is what GitHub matches against its registration;
        `post_auth_uri` is where the user lands afterwards and rides inside signed state.
        Both are resolved by the caller, which keeps this side free of FastAPI — these
        classes run in workers too.
        """
        client_id = SettingsProvider.sget().KEY_GITHUB_CLIENT_ID
        scopes = SettingsProvider.sget().KEY_GITHUB_SCOPES
        state_b64 = new_oauth_state(ctx=ctx, name=name, redirect_uri=post_auth_uri).to_base64()
        # GitHub doesn't need response_type=code (unlike other OAuth services)
        # prompt=select_account forces the account picker for multi-account users
        return (
            "https://github.com/login/oauth/authorize?"
            f"client_id={client_id}&"
            f"redirect_uri={urllib.parse.quote_plus(callback_url)}&"
            f"scope={scopes}&"
            f"state={urllib.parse.quote_plus(state_b64)}&"
            f"prompt=select_account"
        )

    @staticmethod
    async def connect(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        req: IntegrationConnectResolved,
        decrypt: bool = False,
    ) -> IntegrationConnectResult:
        """GitHub has no creds to turn into auth — the user has to visit GitHub.

        So this returns the consent URL instead, and `/github/oauth-callback` writes the
        auth when they come back.
        """
        return IntegrationConnectResult(
            redirect_url=GitHubAuth.build_authorize_url(
                ctx=ctx,
                name=req.name,
                callback_url=req.callback_url or "",
                post_auth_uri=req.post_auth_uri or "",
            )
        )

    @staticmethod
    async def create_auth(ctx: Context, app_creds: GitHubAccessTokenRq) -> OAuthModel | Exception:
        try:
            access_token = await GitHub.access_token(ctx=ctx, rq=app_creds)
            if isinstance(access_token, Exception):
                return access_token

            oauth_model = OAuthModel(
                access_token=access_token.access_token,
                token_type=access_token.token_type,
                scope=access_token.scope,
                expires_in=0,  # GitHub tokens don't expire
            )
            return oauth_model
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error creating GitHub auth: {str(e)}")
            return e

    @staticmethod
    async def revoke_external_auth(
        ctx: Context,
        app_creds: GitHubCreds | None,
        app_auth: OAuthModel | None,
    ) -> bool | Exception:
        """Disconnect GitHub authorization on the provider side.

        This revokes the OAuth grant on GitHub using the app's client_id/client_secret.
        Idempotent: if auth/token is missing, returns True.
        """
        _ = app_creds  # unused
        try:
            if app_auth is None or not app_auth.access_token:
                return True

            logger.bind(ctx=ctx).info("Revoking GitHub grant (provider-side)")
            return await GitHub.delete_grant(ctx=ctx, access_token=app_auth.access_token)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error removing GitHub app: {str(e)}")
            return e

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: GitHubCreds, app_auth: OAuthModel
    ) -> OAuthModel | Exception:
        # GitHub tokens don't expire, return existing auth
        logger.bind(ctx=ctx).info("GitHub tokens don't expire, returning existing auth")
        return app_auth

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: OAuthModel) -> bool:
        # GitHub tokens don't expire
        logger.bind(ctx=ctx).info("GitHub is_refresh_required: False (tokens don't expire)")
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: OAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.access_token}"}

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: GitHubCreds, app_auth: OAuthModel, url: str
    ) -> str:
        logger.bind(ctx=ctx, new_url=url).info("Updating url for GitHub")
        try:
            http_url = HttpUrl(url)
            github_url = "https://api.github.com"
            url = url.replace(f"{http_url.scheme}://{http_url.host}", github_url)
            return url
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Get updated url of GitHub: {str(e)}")
            return url
