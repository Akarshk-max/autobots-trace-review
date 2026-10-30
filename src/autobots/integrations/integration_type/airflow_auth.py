from typing import Dict, Optional, Type

from loguru import logger
from pydantic import BaseModel, HttpUrl

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class AirflowCredsModel(BaseModel):
    """Token + base URL creds.

    This matches Astronomer Workspace "API Token" / similar patterns where requests are:
      Authorization: Bearer <token>
    """

    api_domain: str
    api_key: str


class AirflowAuthModel(BaseModel):
    api_domain: str
    bearer_token: str


class AirflowAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.airflow

    @staticmethod
    async def get_api_domain(app_creds: AirflowCredsModel | None) -> str:
        return str(app_creds.api_domain) if app_creds else ""

    @staticmethod
    def get_creds_model_type() -> Type[AirflowCredsModel]:
        return AirflowCredsModel

    @staticmethod
    def get_auth_model_type() -> Type[AirflowAuthModel]:
        return AirflowAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: AirflowCredsModel) -> AirflowAuthModel | Exception:
        # Validate the domain is a URL (same style as jira_auth.py)
        assert HttpUrl(app_creds.api_domain)
        return AirflowAuthModel(api_domain=app_creds.api_domain, bearer_token=app_creds.api_key)

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: AirflowCredsModel | None, app_auth: AirflowAuthModel | None
    ) -> AirflowAuthModel | Exception:
        # Token is static; no refresh semantics.
        if app_auth:
            return app_auth
        if not app_creds:
            return Exception("Missing Airflow creds")
        return await AirflowAuth.create_auth(ctx=ctx, app_creds=app_creds)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: AirflowAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: AirflowAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        logger.bind(ctx=ctx, token=app_auth.bearer_token[-5:]).info("Generating auth header for Airflow")
        return {"Authorization": f"Bearer {app_auth.bearer_token}"}

    @staticmethod
    async def get_updated_url(
        ctx: Context,
        app_creds: AirflowCredsModel | None,
        app_auth: AirflowAuthModel | None,
        url: str,
    ) -> str:
        # Prefer creds domain, fallback to auth domain.
        base = None
        if app_creds and getattr(app_creds, "api_domain", None):
            base = app_creds.api_domain
        elif app_auth and getattr(app_auth, "api_domain", None):
            base = app_auth.api_domain

        if not base:
            return url

        http_url = HttpUrl(url)
        if http_url.host == "api.astronomer.io":
            return url
        http_url_base = HttpUrl(base)
        base_path = (http_url_base.path or "").rstrip("/")
        new_url = url.replace(http_url.scheme, http_url_base.scheme)
        new_url = new_url.replace(http_url.host, http_url_base.host)
        if base_path:
            # Prepend base path: /dpngcsmm/ + /api/v1/health → /dpngcsmm/api/v1/health
            src_path = http_url.path or "/"
            new_url = new_url.replace(src_path, base_path + src_path, 1)
        return new_url
