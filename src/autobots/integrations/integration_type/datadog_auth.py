from typing import ClassVar, Dict, Literal, Optional, Type

from pydantic import BaseModel, Field

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC

DatadogSite = Literal[
    "datadoghq.com",
    "datadoghq.eu",
    "us3.datadoghq.com",
    "us5.datadoghq.com",
    "ap1.datadoghq.com",
    "ddog-gov.com",
]


class DatadogCredsModel(BaseModel):
    api_key: str = Field(..., min_length=1)
    app_key: str = Field(..., min_length=1)
    site: DatadogSite = "datadoghq.com"


class DatadogAuthModel(BaseModel):
    api_key: str = Field(..., min_length=1)
    app_key: str = Field(..., min_length=1)
    site: DatadogSite = "datadoghq.com"


class DatadogAuth(IntegrationTypeABC):
    DEFAULT_SITE: ClassVar[DatadogSite] = "datadoghq.com"

    SITE_TO_HOST: ClassVar[Dict[DatadogSite, str]] = {
        "datadoghq.com": "https://api.datadoghq.com",
        "datadoghq.eu": "https://api.datadoghq.eu",
        "us3.datadoghq.com": "https://api.us3.datadoghq.com",
        "us5.datadoghq.com": "https://api.us5.datadoghq.com",
        "ap1.datadoghq.com": "https://api.ap1.datadoghq.com",
        "ddog-gov.com": "https://api.ddog-gov.com",
    }

    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.datadog

    @classmethod
    def _resolve_host_from_site(cls, site: DatadogSite) -> str:
        # With Literal, this is always present, but keep .get() anyway for safety.
        return cls.SITE_TO_HOST.get(site, cls.SITE_TO_HOST[cls.DEFAULT_SITE]).rstrip("/")

    @staticmethod
    async def get_api_domain(app_creds: DatadogCredsModel | None) -> str:
        if not app_creds:
            return DatadogAuth.SITE_TO_HOST[DatadogAuth.DEFAULT_SITE]
        return DatadogAuth._resolve_host_from_site(app_creds.site)

    @staticmethod
    def get_creds_model_type() -> Type[DatadogCredsModel]:
        return DatadogCredsModel

    @staticmethod
    def get_auth_model_type() -> Type[DatadogAuthModel]:
        return DatadogAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: DatadogCredsModel) -> DatadogAuthModel | Exception:
        return DatadogAuthModel(
            api_key=app_creds.api_key,
            app_key=app_creds.app_key,
            site=app_creds.site,
        )

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: DatadogCredsModel, app_auth: DatadogAuthModel
    ) -> DatadogAuthModel | Exception:
        return app_auth if app_auth else await DatadogAuth.create_auth(ctx, app_creds)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: DatadogAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: DatadogAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {
            "DD-API-KEY": app_auth.api_key,
            "DD-APPLICATION-KEY": app_auth.app_key,
            "Content-Type": "application/json",
        }

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: DatadogCredsModel, app_auth: DatadogAuthModel, url: str
    ) -> str:
        if url.startswith("http://") or url.startswith("https://"):
            return url
        base = DatadogAuth._resolve_host_from_site(app_auth.site)
        if not url.startswith("/"):
            url = f"/{url}"
        return f"{base}{url}"