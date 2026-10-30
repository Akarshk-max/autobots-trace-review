from enum import Enum
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import (
    AppCredsModelType,
    IntegrationModelType,
    IntegrationTypeABC,
)


class PlaidEnvironment(str, Enum):
    sandbox = "sandbox"
    development = "development"
    production = "production"


class PlaidItemModel(BaseModel):
    """A single bank connection (Item) from Plaid Link."""

    access_token: str
    item_id: str
    institution_id: str | None = None
    institution_name: str | None = None


class PlaidAuthModel(BaseModel):
    client_id: str
    secret: str
    environment: PlaidEnvironment = PlaidEnvironment.sandbox
    items: List[PlaidItemModel] = []


class PlaidAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.plaid

    @staticmethod
    async def get_api_domain(app_creds: PlaidAuthModel | None) -> str:
        if app_creds and app_creds.environment == PlaidEnvironment.production:
            return "https://production.plaid.com"
        elif app_creds and app_creds.environment == PlaidEnvironment.development:
            return "https://development.plaid.com"
        return "https://sandbox.plaid.com"

    @staticmethod
    def get_creds_model_type() -> Type[AppCredsModelType]:
        return PlaidAuthModel

    @staticmethod
    def get_auth_model_type() -> Type[IntegrationModelType]:
        return PlaidAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: PlaidAuthModel) -> PlaidAuthModel | Exception:
        return PlaidAuthModel(
            client_id=app_creds.client_id,
            secret=app_creds.secret,
            environment=app_creds.environment,
        )

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: PlaidAuthModel, app_auth: PlaidAuthModel
    ) -> PlaidAuthModel | Exception:
        if app_auth:
            return PlaidAuthModel(
                client_id=app_creds.client_id,
                secret=app_creds.secret,
                environment=app_creds.environment,
                items=app_auth.items,
            )
        return PlaidAuthModel(
            client_id=app_creds.client_id,
            secret=app_creds.secret,
            environment=app_creds.environment,
        )

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: PlaidAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: PlaidAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {
            "PLAID-CLIENT-ID": app_auth.client_id,
            "PLAID-SECRET": app_auth.secret,
            "Content-Type": "application/json",
        }

    @staticmethod
    async def get_updated_body(
        ctx: Context, app_auth: PlaidAuthModel, body: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        if not body or not app_auth or not app_auth.items:
            return body
        body = dict(body)
        item_id = body.pop("item_id", None)
        if item_id:
            item = next((i for i in app_auth.items if i.item_id == item_id), None)
        else:
            item = app_auth.items[0]
        if item:
            body["access_token"] = item.access_token
        return body
