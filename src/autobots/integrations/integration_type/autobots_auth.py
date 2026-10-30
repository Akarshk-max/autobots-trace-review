from datetime import datetime, timedelta
from typing import Dict, Optional, Type

from pydantic import BaseModel

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.auth.auth import get_auth
from src.autobots.core.clock.clock import Clock
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class AutobotsCredsModel(BaseModel):
    email: str
    password: str


class AutobotsAuthModel(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int
    created_at: datetime


class AutobotsAuth(IntegrationTypeABC[AutobotsCredsModel, AutobotsAuthModel]):

    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.autobots

    @staticmethod
    async def get_api_domain(app_creds: AutobotsCredsModel | None) -> str:
        return SettingsProvider.sget().AUTOBOTS_API_URL

    @staticmethod
    def get_creds_model_type() -> Type[AutobotsCredsModel]:
        return AutobotsCredsModel

    @staticmethod
    def get_auth_model_type() -> Type[AutobotsAuthModel]:
        return AutobotsAuthModel

    @staticmethod
    async def create_auth(
        ctx: Context, app_creds: AutobotsCredsModel
    ) -> AutobotsAuthModel:
        auth_res = await get_auth().sign_in_with_password(app_creds.email, app_creds.password)
        return AutobotsAuthModel(
            access_token=auth_res.session.access_token,
            refresh_token=auth_res.session.refresh_token,
            expires_in=auth_res.session.expires_in,
            created_at=Clock.datetime_now(),
        )

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: AutobotsAuthModel) -> bool:
        expires_at = app_auth.created_at + timedelta(seconds=app_auth.expires_in)
        return Clock.datetime_now() + timedelta(seconds=60) >= expires_at

    @staticmethod
    async def refresh_auth(
        ctx: Context,
        app_creds: AutobotsCredsModel | None,
        app_auth: AutobotsAuthModel | None,
    ) -> AutobotsAuthModel:
        if app_auth and app_auth.refresh_token:
            auth_res = await get_auth().refresh_session(app_auth.refresh_token)
            return AutobotsAuthModel(
                access_token=auth_res.session.access_token,
                refresh_token=auth_res.session.refresh_token,
                expires_in=auth_res.session.expires_in,
                created_at=Clock.datetime_now(),
            )
        if app_creds:
            return await AutobotsAuth.create_auth(ctx, app_creds)
        
        raise ValueError("Cannot refresh Autobots auth: missing both app_auth and app_creds")


    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: AutobotsAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.access_token}"}
