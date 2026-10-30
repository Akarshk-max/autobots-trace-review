import base64
from typing import Dict, Optional, Type

from pydantic import BaseModel, EmailStr, HttpUrl

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class ZendeskCredsModel(BaseModel):
    api_domain: str
    api_key: str
    email: EmailStr


class ZendeskAuthModel(BaseModel):
    api_domain: str
    basic_token: str


# https://support.zendesk.com/hc/en-us/articles/4408831452954-How-can-I-authenticate-API-requests
class ZendeskAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.zendesk

    @staticmethod
    async def get_api_domain(app_creds: ZendeskCredsModel | None) -> str:
        return app_creds.api_domain

    @staticmethod
    def get_creds_model_type() -> Type[ZendeskCredsModel]:
        return ZendeskCredsModel

    @staticmethod
    def get_auth_model_type() -> Type[ZendeskAuthModel]:
        return ZendeskAuthModel

    @staticmethod
    async def create_auth(
        ctx: Context, app_creds: ZendeskCredsModel
    ) -> ZendeskAuthModel | Exception:
        assert HttpUrl(app_creds.api_domain)
        api_key_bytes = f"{app_creds.email}/token:{app_creds.api_key}".encode("utf-8")
        basic_auth_token = base64.b64encode(api_key_bytes)
        return ZendeskAuthModel(basic_token=basic_auth_token, api_domain=app_creds.api_domain)

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: ZendeskCredsModel, app_auth: ZendeskAuthModel
    ) -> ZendeskAuthModel | Exception:
        return await ZendeskAuth.create_auth(ctx=ctx, app_creds=app_creds)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: ZendeskAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: ZendeskAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Basic {app_auth.basic_token}"}
