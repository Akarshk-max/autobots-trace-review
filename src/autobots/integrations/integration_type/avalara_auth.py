import base64
from typing import Dict, Optional, Type

from pydantic import BaseModel

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class AvalaraCredsModel(BaseModel):
    account_id: str
    license_key: str


class AvalaraAuthModel(BaseModel):
    basic_token: str


class AvalaraAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.avalara

    @staticmethod
    async def get_api_domain(app_creds: AvalaraCredsModel | None) -> str:
        return "https://rest.avatax.com"

    @staticmethod
    def get_creds_model_type() -> Type[AvalaraCredsModel]:
        return AvalaraCredsModel

    @staticmethod
    def get_auth_model_type() -> Type[AvalaraAuthModel]:
        return AvalaraAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: AvalaraCredsModel) -> AvalaraAuthModel | Exception:
        raw = f"{app_creds.account_id}:{app_creds.license_key}".encode("utf-8")
        basic_token = base64.b64encode(raw).decode("utf-8")
        return AvalaraAuthModel(basic_token=basic_token)

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: AvalaraCredsModel, app_auth: AvalaraAuthModel
    ) -> AvalaraAuthModel | Exception:
        return app_auth if app_auth else await AvalaraAuth.create_auth(ctx=ctx, app_creds=app_creds)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: AvalaraAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: AvalaraAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Basic {app_auth.basic_token}"}
