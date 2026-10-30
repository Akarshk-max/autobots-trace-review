import urllib.parse
from typing import Any, Dict, Literal, Optional, Type

from fastapi import Response
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots import SettingsProvider
from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.api.v1.api_auth.oauth_state import new_oauth_state
from src.autobots.conn.only_auth.slack.slack import Slack
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import (
    IntegrationConnectResolved,
    IntegrationConnectResult,
)
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC
from src.autobots.user.user_orm_model import UserORM


class SlackTeam(BaseModel):
    id: str
    name: str


class SlackAuthedUser(BaseModel):
    id: str
    scope: str
    access_token: str
    token_type: Literal["user"]


class SlackCreds(BaseModel):
    pass


class SlackAuthModel(BaseModel):
    app_id: str
    scope: str
    token_type: Literal["bot"]
    access_token: str
    bot_user_id: str
    team: SlackTeam
    enterprise: str | None = None
    is_enterprise_install: bool = False
    authed_user: SlackAuthedUser


class SlackAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.slack

    @staticmethod
    def oauth_callback_path() -> str:
        """Where Slack sends the browser back. Registered in its console."""
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_PATH_SLACK}/oauth-callback"

    @staticmethod
    def build_authorize_url(
        ctx: Context, name: str, callback_url: str, post_auth_uri: str
    ) -> str:
        """The consent URL, built here so the route and connect() cannot drift.

        `callback_url` is ours and is what the provider matches against its registration;
        `post_auth_uri` is where the user lands afterwards and rides inside signed state.
        """
        client_id = SettingsProvider.sget().SLACK_CLIENT_ID
        state_b64 = new_oauth_state(ctx=ctx, name=name, redirect_uri=post_auth_uri).to_base64()
        url_encoded_callback_uri = urllib.parse.quote_plus(callback_url)
        url_encoded_scope = urllib.parse.quote_plus(SettingsProvider.sget().SLACK_SCOPE)
        url_encoded_user_scope = urllib.parse.quote_plus(SettingsProvider.sget().SLACK_USER_SCOPE)
        return (
            f"{Slack.AUTHORIZATION_CODE_URL}?"
            f"scope={url_encoded_scope}&"
            f"user_scope={url_encoded_user_scope}&"
            f"redirect_uri={url_encoded_callback_uri}&"
            f"client_id={client_id}&"
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
            redirect_url=SlackAuth.build_authorize_url(
                ctx=ctx,
                name=req.name,
                callback_url=req.callback_url or "",
                post_auth_uri=req.post_auth_uri or "",
            )
        )

    @staticmethod
    async def get_api_domain(app_creds: SlackCreds | None) -> str:
        return "https://slack.com"

    @staticmethod
    def get_creds_model_type() -> Type[SlackCreds]:
        return SlackCreds

    @staticmethod
    def get_auth_model_type() -> Type[SlackAuthModel]:
        return SlackAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: SlackCreds) -> SlackAuthModel | Exception:
        pass  # return SlackResponse(api_key=app_creds.api_key)

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: SlackCreds, app_auth: SlackAuthModel
    ) -> SlackAuthModel | Exception:
        pass  # return app_auth if app_auth else ApiKeyAuthModel(api_key=app_creds.api_key)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: SlackAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: SlackAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        # token = app_auth.authed_user.access_token
        token = app_auth.access_token
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    async def handle_webhook_challenge(
        ctx: Context, event_data: Dict[str, Any]
    ) -> Response | None:
        if event_data and event_data.get("type") and event_data.get("type") == "url_verification":
            logger.bind(ctx=ctx).info("Slack URL verification challenge received")
            return Response(content=event_data.get("challenge"), media_type="text/plain")
        return None
