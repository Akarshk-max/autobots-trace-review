import re
from typing import Dict, Optional, Type
from urllib.parse import urlsplit, urlunsplit

from loguru import logger
from pydantic import BaseModel

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.conn.only_auth.netsuite.netsuite_client import NetSuite, NetSuiteOAuth1
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class NetSuiteCreds(BaseModel):
    """
    For NetSuite TBA OAuth 1.0 (like Postman):
    - realm = account_id
    - HMAC-SHA256
    """

    account_id: str  # realm
    consumer_key: str
    consumer_secret: str
    access_token: str
    token_secret: str


_NETSUITE_HOST_RE = re.compile(
    r"^(https://)([^.]+)(\.suitetalk\.api\.netsuite\.com)(/.*)?$",
    re.IGNORECASE,
)


def normalize_netsuite_url(url: str, account_id: str) -> str:
    """
    Replace the NetSuite account segment in:
      https://<anything>.suitetalk.api.netsuite.com/...
    with the provided account_id.
    """
    m = _NETSUITE_HOST_RE.match(url.strip())
    if not m:
        return url

    prefix, _old_acct, suffix, path = m.groups()
    return f"{prefix}{account_id}{suffix}{path or ''}"


class NetSuiteAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.netsuite

    @staticmethod
    async def get_api_domain(app_creds: NetSuiteCreds | None):
        if not app_creds:
            return None
        return (NetSuite.derive_base_url(app_creds.account_id)).rstrip("/")

    @staticmethod
    def get_creds_model_type() -> Type[NetSuiteCreds]:
        return NetSuiteCreds

    @staticmethod
    def get_auth_model_type() -> Type[NetSuiteOAuth1]:
        return NetSuiteOAuth1

    @staticmethod
    async def create_auth(ctx: Context, app_creds: NetSuiteCreds) -> NetSuiteOAuth1 | Exception:
        try:
            res = NetSuiteOAuth1(
                account_id=app_creds.account_id,
                consumer_key=app_creds.consumer_key,
                consumer_secret=app_creds.consumer_secret,
                access_token=app_creds.access_token,
                token_secret=app_creds.token_secret,
            )
            logger.bind(ctx=ctx, account_id=app_creds.account_id).info(
                "Created NetSuite OAuth1 auth"
            )
            return res
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error creating NetSuite auth: {str(e)}")
            return e

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: NetSuiteCreds | None, app_auth: NetSuiteOAuth1 | None
    ) -> NetSuiteOAuth1 | Exception:
        """
        No refresh flow for NetSuite TBA tokens.
        Return current auth, or rebuild from creds if needed.
        """
        if app_auth:
            return app_auth
        if not app_creds:
            return Exception("NetSuite credentials are required")
        return await NetSuiteAuth.create_auth(ctx=ctx, app_creds=app_creds)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: NetSuiteOAuth1) -> bool:
        """
        NetSuite TBA OAuth1 tokens are long-lived; treat as non-expiring.
        """
        logger.bind(ctx=ctx).info("is_refresh_required for NetSuite: False (TBA tokens)")
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: NetSuiteOAuth1, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        """
        If method/url are provided, return a signed OAuth1 header.
        Otherwise, returns a minimal header placeholder (not valid for actual NetSuite calls).
        """
        if config.method and config.url:
            url = normalize_netsuite_url(config.url, app_auth.account_id)
            assert url
            return NetSuite.build_oauth1_header(
                ctx=ctx,
                auth=app_auth,
                method=config.method,
                url=url,
                params=config.params,
                body=config.body,
                include_body_params=False,
            )

        logger.bind(ctx=ctx).warning(
            "NetSuite get_auth_header called without method/url; OAuth1 signature cannot be computed"
        )
        return {"Authorization": 'OAuth realm="%s"' % app_auth.account_id}

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: NetSuiteCreds, app_auth: NetSuiteOAuth1, url: str
    ) -> str:
        logger.bind(ctx=ctx, original_url=url).info("Updating NetSuite URL")
        try:
            url = normalize_netsuite_url(url, app_auth.account_id)

            base = await NetSuiteAuth.get_api_domain(app_creds)
            if base:
                parts = urlsplit(url)
                base_parts = urlsplit(base)
                updated = urlunsplit(
                    (
                        base_parts.scheme,
                        base_parts.netloc,
                        parts.path,
                        parts.query,
                        parts.fragment,
                    )
                )
                logger.bind(ctx=ctx, updated_url=updated).info("Updated NetSuite URL")
                return updated

            logger.bind(ctx=ctx, updated_url=url).info("Updated NetSuite URL")
            return url
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error updating NetSuite URL: {str(e)}")
            return url
