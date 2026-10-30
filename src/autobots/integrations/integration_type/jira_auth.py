import base64
from typing import Dict, Optional, Type

from pydantic import BaseModel, EmailStr, HttpUrl

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class JiraCredsModel(BaseModel):
    api_domain: str
    api_key: str
    email: EmailStr


class JiraAuthModel(BaseModel):
    api_domain: str
    basic_token: str


class JiraAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.jira

    @staticmethod
    async def get_api_domain(app_creds: JiraCredsModel | None) -> str:
        return str(app_creds.api_domain)

    @staticmethod
    def get_creds_model_type() -> Type[JiraCredsModel]:
        return JiraCredsModel

    @staticmethod
    def get_auth_model_type() -> Type[JiraAuthModel]:
        return JiraAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: JiraCredsModel) -> JiraAuthModel | Exception:
        assert HttpUrl(app_creds.api_domain)
        api_key_bytes = f"{app_creds.email}:{app_creds.api_key}".encode("utf-8")
        basic_auth_token = base64.b64encode(api_key_bytes)
        return JiraAuthModel(basic_token=basic_auth_token, api_domain=app_creds.api_domain)

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: JiraCredsModel, app_auth: JiraAuthModel
    ) -> JiraAuthModel | Exception:
        return app_auth if app_auth else JiraAuth.create_auth(ctx=ctx, app_creds=app_creds)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: JiraAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: JiraAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Basic {app_auth.basic_token}"}

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: JiraCredsModel, app_auth: JiraAuthModel, url: str
    ) -> str:
        http_url1 = HttpUrl(url)
        http_url2 = HttpUrl(app_auth.api_domain)
        new_url = url.replace(http_url1.scheme, http_url2.scheme)
        new_url = new_url.replace(http_url1.host, http_url2.host)
        return new_url
