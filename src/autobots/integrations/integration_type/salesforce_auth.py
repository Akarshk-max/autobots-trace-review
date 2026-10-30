import urllib.parse
from typing import Dict, Optional, Type

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, HttpUrl

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.api.v1.api_auth.oauth_state import new_oauth_state
from src.autobots.conn.only_auth.salesforce.salesforce_client import (
    Salesforce,
    SalesforceAccessToken,
    SalesforceAccessTokenRq,
    SalesforceRefreshedAccessToken,
    SalesforceRefreshTokenRq,
)
from src.autobots.core.clock.clock import Clock
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import (
    IntegrationConnectResolved,
    IntegrationConnectResult,
    OAuthModel,
)
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC
from src.autobots.user.user_orm_model import UserORM


class SalesforceCreds(BaseModel):
    instance_url: str


class SalesforceAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.salesforce

    @staticmethod
    def oauth_callback_path() -> str:
        """Where the provider sends the browser back. Registered in its console."""
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}/salesforce/oauth-callback"

    @staticmethod
    def build_authorize_url(
        ctx: Context,
        name: str,
        callback_url: str,
        post_auth_uri: str,
        app_creds: "SalesforceCreds | None" = None,
    ) -> str:
        """The consent URL, built here so the route and connect() cannot drift.

        This provider's authorize host comes from the customer's own credentials, so
        unlike the settings-only providers it cannot build a URL without them.
        """
        if not app_creds:
            raise HTTPException(400, "Salesforce credentials not found.")
        instance_url = app_creds.instance_url
        is_sandbox = "sandbox" in instance_url
        # Use the customer's My Domain so enterprise SSO / JumpCloud federation is
        # honoured. Sending users to login.salesforce.com bypasses the org's IdP.
        authorization_code_url = Salesforce.authorize_url(instance_url, is_sandbox)
        settings = SettingsProvider.sget()
        state_b64 = new_oauth_state(ctx=ctx, name=name, redirect_uri=post_auth_uri).to_base64()
        return (
            f"{authorization_code_url}?"
            f"client_id={urllib.parse.quote_plus(settings.SALESFORCE_CLIENT_ID)}&"
            f"redirect_uri={urllib.parse.quote_plus(callback_url)}&"
            f"response_type=code&"
            f"scope={urllib.parse.quote_plus(settings.SALESFORCE_SCOPES)}&"
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
            SalesforceCreds.model_validate(creds_doc.creds) if creds_doc and creds_doc.creds else None
        )
        return IntegrationConnectResult(
            redirect_url=SalesforceAuth.build_authorize_url(
                ctx=ctx,
                name=req.name,
                callback_url=req.callback_url or "",
                post_auth_uri=req.post_auth_uri or "",
                app_creds=app_creds,
            )
        )

    @staticmethod
    async def get_api_domain(app_creds: SalesforceCreds | None):
        return app_creds.instance_url if app_creds else "https://login.salesforce.com"

    @staticmethod
    def get_creds_model_type() -> Type[SalesforceCreds]:
        return SalesforceCreds

    @staticmethod
    def get_auth_model_type() -> Type[OAuthModel]:
        return OAuthModel

    @staticmethod
    async def create_auth(
        ctx: Context,
        app_creds: SalesforceAccessTokenRq,
        is_sandbox: bool = False,
        instance_url: str | None = None,
    ) -> OAuthModel | Exception:
        access_token = await Salesforce.access_token(
            ctx=ctx, rq=app_creds, is_sandbox=is_sandbox, instance_url=instance_url
        )
        match access_token:
            case SalesforceAccessToken():
                res = OAuthModel.model_validate(access_token.model_dump())
                res.code = app_creds.code
                return res
            case Exception():
                return access_token

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: SalesforceCreds | None, app_auth: OAuthModel | None
    ) -> OAuthModel | Exception:
        settings = SettingsProvider.sget()
        token_rq = SalesforceRefreshTokenRq(
            client_id=settings.SALESFORCE_CLIENT_ID,
            client_secret=settings.SALESFORCE_CLIENT_SECRET,
            refresh_token=app_auth.refresh_token,
        )
        is_sandbox = "sandbox" in app_creds.instance_url

        refreshed_access_token = await Salesforce.refresh_access_token(
            ctx, token_rq, is_sandbox, instance_url=app_creds.instance_url
        )
        match refreshed_access_token:
            case SalesforceRefreshedAccessToken():
                new_auth_model = OAuthModel.model_validate(app_auth)
                new_auth_model.access_token = refreshed_access_token.access_token
                new_auth_model.expires_in = refreshed_access_token.expires_in
                new_auth_model.created_at = Clock.datetime_now()
                logger.bind(ctx=ctx).info("Refreshed token for Salesforce")
                return new_auth_model
            case Exception():
                logger.bind(ctx=ctx).error("Unable to refresh token for Salesforce")
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
        ctx: Context, app_creds: SalesforceCreds, app_auth: OAuthModel, url: str
    ) -> str:
        logger.bind(ctx=ctx, new_url=url).info("Updating url")
        try:
            if not url.startswith("http"):
                url = f"https://{url}"
            http_url = HttpUrl(url)

            salesforce_url = await SalesforceAuth.get_api_domain(app_creds)
            url = url.replace(f"{http_url.scheme}://{http_url.host}", salesforce_url)
            return url
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Get updated url of Salesforce: {str(e)}")
