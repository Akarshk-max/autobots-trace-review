from typing import Dict, Literal, Optional, Type

from fastapi import HTTPException
from intuitlib.client import AuthClient
from intuitlib.enums import Scopes
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, HttpUrl

from src.autobots import SettingsProvider
from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.api.v1.api_auth.oauth_state import new_oauth_state
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


class IntuitCreds(BaseModel):
    environment: Literal[
        "sandbox",
        "production"
    ] = "sandbox"  # "sandbox" | "production"


class IntuitAuthModel(OAuthModel):
    realm_id: str | None = None


class IntuitAuth(IntegrationTypeABC):
    MINOR_VERSION: int = 73

    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.intuit

    @staticmethod
    def oauth_callback_path() -> str:
        """Where Intuit sends the browser back. Registered on the Intuit app."""
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_PATH_INTUIT}/oauth-callback"

    @staticmethod
    def auth_client(environment: str, callback_url: str) -> AuthClient:
        """Intuit ships its own client, and sandbox and production are different apps."""
        settings = SettingsProvider.sget()
        if environment == "sandbox":
            return AuthClient(
                client_id=settings.INTUIT_SANDBOX_CLIENT_ID,
                client_secret=settings.INTUIT_SANDBOX_CLIENT_SECRET,
                redirect_uri=callback_url,
                environment=environment,
            )
        return AuthClient(
            client_id=settings.INTUIT_CLIENT_ID,
            client_secret=settings.INTUIT_CLIENT_SECRET,
            redirect_uri=callback_url,
            environment=environment,
        )

    @staticmethod
    def build_authorize_url(
        ctx: Context,
        name: str,
        callback_url: str,
        post_auth_uri: str,
        app_creds: "IntuitCreds | None" = None,
    ) -> str:
        """The consent URL, from Intuit's own SDK rather than assembled by hand."""
        if not app_creds:
            raise HTTPException(400, "Intuit credentials not configured")
        client = IntuitAuth.auth_client(app_creds.environment, callback_url)
        state_b64 = new_oauth_state(ctx=ctx, name=name, redirect_uri=post_auth_uri).to_base64()
        return client.get_authorization_url([Scopes.ACCOUNTING, Scopes.PAYMENT], state_token=state_b64)

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
            IntuitCreds.model_validate(creds_doc.creds) if creds_doc and creds_doc.creds else None
        )
        return IntegrationConnectResult(
            redirect_url=IntuitAuth.build_authorize_url(
                ctx=ctx,
                name=req.name,
                callback_url=req.callback_url or "",
                post_auth_uri=req.post_auth_uri or "",
                app_creds=app_creds,
            )
        )

    @staticmethod
    async def get_api_domain(app_creds: IntuitCreds | None) -> str | None:
        if not app_creds:
            return None
        return (
            "https://sandbox-quickbooks.api.intuit.com"
            if app_creds.environment == "sandbox"
            else "https://quickbooks.api.intuit.com"
        )

    @staticmethod
    def get_creds_model_type() -> Type[IntuitCreds]:
        return IntuitCreds

    @staticmethod
    def get_auth_model_type() -> Type[IntuitAuthModel]:
        return IntuitAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: IntuitCreds) -> IntuitAuthModel | Exception:
        pass

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: IntuitCreds, app_auth: IntuitAuthModel
    ) -> IntuitAuthModel | Exception:
        if app_creds.environment == "sandbox":
            auth_client = AuthClient(
                client_id=SettingsProvider.sget().INTUIT_SANDBOX_CLIENT_ID,
                client_secret=SettingsProvider.sget().INTUIT_SANDBOX_CLIENT_SECRET,
                redirect_uri=SettingsProvider.sget().INTUIT_REDIRECT_URL,
                environment=app_creds.environment,  # “sandbox” or “production”
            )
        else:
            auth_client = AuthClient(
                client_id=SettingsProvider.sget().INTUIT_CLIENT_ID,
                client_secret=SettingsProvider.sget().INTUIT_CLIENT_SECRET,
                redirect_uri=SettingsProvider.sget().INTUIT_REDIRECT_URL,
                environment=app_creds.environment,  # “sandbox” or “production”
            )
        auth_client.refresh(app_auth.refresh_token)
        new_auth_model = IntuitAuthModel.model_validate(app_auth)
        new_auth_model.access_token = auth_client.access_token
        new_auth_model.expires_in = auth_client.expires_in
        new_auth_model.created_at = Clock.datetime_now()
        new_auth_model.refresh_token = auth_client.refresh_token
        new_auth_model.refresh_token_expires_in = auth_client.x_refresh_token_expires_in
        logger.bind(ctx=ctx).info("Refreshed token for Intuit")
        return new_auth_model

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: IntuitAuthModel) -> bool:
        curr_time = Clock.datetime_now()
        auth_creation_time = app_auth.created_at
        duration_since_creation = curr_time - auth_creation_time
        duration_sec = duration_since_creation.total_seconds()
        is_refresh = abs(duration_sec) > app_auth.expires_in - 600
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
        ctx: Context, app_auth: IntuitAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.access_token}"}

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: IntuitCreds, app_auth: IntuitAuthModel, url: str
    ) -> str:
        logger.bind(ctx=ctx, new_url=url).info("Updating url")
        try:
            http_url = HttpUrl(url)

            intuit_url = await IntuitAuth.get_api_domain(app_creds)
            url = url.replace(f"{http_url.scheme}://{http_url.host}", intuit_url)

            path_parts = http_url.path.split("/")
            for i in range(len(path_parts)):
                if path_parts[i] == "company":
                    user_company_part = i + 1
                    path_parts[user_company_part] = app_auth.realm_id
            new_path = "/".join(path_parts)
            new_url = url.replace(http_url.path, new_path)
            new_url += f"?minorversion={IntuitAuth.MINOR_VERSION}"
            logger.bind(ctx=ctx, new_url=new_url).info("Updated url")
            return new_url
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Get updated url of Intuit: {str(e)}")