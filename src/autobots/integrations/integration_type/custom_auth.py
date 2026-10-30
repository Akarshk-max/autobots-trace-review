import re
from typing import Dict, Optional, Type
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, field_validator

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC

HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class CustomAuthModel(BaseModel):
    """Connect to an API we don't have a built-in integration for.

    Base Url is optional. Set it and every request through this connection is sent
    to that host, keeping the path from the action. Leave it blank to let each
    action use the host in its own URL.

    Api Key is the credential we send. By default it goes out as
    `Authorization: Bearer <key>` - change Header Name and Header Value Prefix if
    the API expects something else, for example `X-Api-Key` with an empty prefix.
    """

    base_url: str = ""
    api_key: str
    header_name: str = "Authorization"
    header_value_prefix: str = "Bearer"

    @field_validator("header_name")
    @classmethod
    def validate_header_name(cls, header_name: str) -> str:
        if not HEADER_NAME_RE.match(header_name or ""):
            raise ValueError(f"Invalid header name: {header_name!r}")
        return header_name

    @field_validator("header_value_prefix")
    @classmethod
    def validate_header_value_prefix(cls, prefix: str) -> str:
        if "\r" in prefix or "\n" in prefix:
            raise ValueError("Header value prefix contains a line break")
        return prefix

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, base_url: str) -> str:
        base_url = base_url.strip()
        if not base_url:
            return base_url
        # urlsplit silently drops these, so it cannot be the one to reject them.
        if any(c in base_url for c in "\r\n\t"):
            raise ValueError("Base url contains a line break")
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"Invalid base url: {base_url!r}")
        return base_url

    def auth_header(self) -> Dict[str, str]:
        prefix = self.header_value_prefix.strip()
        key = self.api_key.strip()
        # A key pasted straight out of curl already carries its scheme.
        if prefix and key.lower().startswith(f"{prefix.lower()} "):
            return {self.header_name: key}
        return {self.header_name: f"{prefix} {key}" if prefix else key}

    def rebase(self, url: str) -> str:
        if not self.base_url:
            return url
        base = urlsplit(self.base_url)
        parts = urlsplit(url)
        path = f"{base.path.rstrip('/')}{parts.path}"
        return urlunsplit((base.scheme, base.netloc, path, parts.query, parts.fragment))


class CustomAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.custom

    @staticmethod
    async def get_api_domain(app_creds: CustomAuthModel | None) -> str:
        return app_creds.base_url if app_creds else ""

    @staticmethod
    def get_creds_model_type() -> Type[CustomAuthModel]:
        return CustomAuthModel

    @staticmethod
    def get_auth_model_type() -> Type[CustomAuthModel]:
        return CustomAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: CustomAuthModel) -> CustomAuthModel | Exception:
        return app_creds.model_copy(deep=True)

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: CustomAuthModel, app_auth: CustomAuthModel
    ) -> CustomAuthModel | Exception:
        return app_auth if app_auth else app_creds.model_copy(deep=True)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: CustomAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: CustomAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return app_auth.auth_header()

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: CustomAuthModel, app_auth: CustomAuthModel, url: str
    ) -> str:
        auth = app_auth or app_creds
        return auth.rebase(url) if auth else url
