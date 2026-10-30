import ssl
from typing import Dict, Optional, Type

import httpx
from loguru import logger
from pydantic import BaseModel, HttpUrl

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.conn.only_auth.adp.adp_client import (
    ADP,
    ADPAccessToken,
    ADPAccessTokenRq,
    _get_cert_paths,
)
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import OAuthModel
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class ADPCreds(BaseModel):
    client_id: str
    client_secret: str
    ssl_cert_path: str  # Required - path to SSL certificate (PEM or PFX)
    ssl_key_path: Optional[str] = None  # Optional if key is embedded in cert file


class ADPAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.adp

    @staticmethod
    async def get_api_domain(app_creds: ADPCreds | None):
        return "https://api.adp.com"

    @staticmethod
    def get_creds_model_type() -> Type[ADPCreds]:
        return ADPCreds

    @staticmethod
    def get_auth_model_type() -> Type[OAuthModel]:
        return OAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: ADPCreds) -> OAuthModel | Exception:
        """Create OAuth model using ADP client credentials flow with mTLS"""
        token_rq = ADPAccessTokenRq(
            client_id=app_creds.client_id,
            client_secret=app_creds.client_secret,
            ssl_cert_path=app_creds.ssl_cert_path,
            ssl_key_path=app_creds.ssl_key_path,
        )

        access_token = await ADP.access_token(ctx=ctx, rq=token_rq)
        match access_token:
            case ADPAccessToken():
                res = OAuthModel(
                    access_token=access_token.access_token,
                    token_type=access_token.token_type,
                    expires_in=access_token.expires_in,
                    created_at=access_token.created_at,
                    scope=access_token.scope,
                    refresh_token=None,  # Client credentials flow doesn't use refresh tokens
                    code=None,
                )
                return res
            case Exception():
                return access_token

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: ADPCreds | None, app_auth: OAuthModel | None
    ) -> OAuthModel | Exception:
        """
        Refresh auth by getting a new token.
        ADP client credentials flow doesn't use refresh tokens,
        so we get a new access token using the stored credentials.
        """
        if not app_creds:
            return Exception("ADP credentials are required for token refresh")

        logger.bind(ctx=ctx).info("Refreshing ADP token by getting new access token")
        return await ADPAuth.create_auth(ctx, app_creds)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: OAuthModel) -> bool:
        """Check if token refresh is required (with 5 minute buffer)"""
        curr_time = Clock.datetime_now()
        auth_creation_time = app_auth.created_at
        duration_since_creation = curr_time - auth_creation_time
        duration_sec = duration_since_creation.total_seconds()
        is_refresh = duration_sec > app_auth.expires_in - 300

        logger.bind(
            ctx=ctx,
            is_refresh_required=is_refresh,
            expires_in=app_auth.expires_in,
            duration_sec=duration_sec,
        ).info("is_refresh_required for ADP")

        return is_refresh

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: OAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        """Create authorization header with Bearer token"""
        return {"Authorization": f"Bearer {app_auth.access_token}"}

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: ADPCreds, app_auth: OAuthModel, url: str
    ) -> str:
        """Update URL to use the correct ADP API domain"""
        logger.bind(ctx=ctx, original_url=url).info("Updating ADP URL")
        try:
            http_url = HttpUrl(url)
            adp_base_url = await ADPAuth.get_api_domain(app_creds)

            if adp_base_url:
                updated_url = url.replace(
                    f"{http_url.scheme}://{http_url.host}", adp_base_url.rstrip("/")
                )
                logger.bind(ctx=ctx, updated_url=updated_url).info("Updated ADP URL")
                return updated_url
            return url

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error updating ADP URL: {str(e)}")
            return url

    @staticmethod
    async def get_ssl_context(ctx: Context, app_creds: ADPCreds | None) -> Optional[ssl.SSLContext]:
        """
        Create SSL context for ADP mTLS authentication.
        ADP requires mutual TLS for all API calls.
        """
        if not app_creds:
            logger.bind(ctx=ctx).warning("No ADP credentials provided for SSL context")
            return None

        try:
            token_rq = ADPAccessTokenRq(
                client_id=app_creds.client_id,
                client_secret=app_creds.client_secret,
                ssl_cert_path=app_creds.ssl_cert_path,
                ssl_key_path=app_creds.ssl_key_path,
            )

            async with _get_cert_paths(ctx, token_rq) as (cert_path, key_path):
                # SSLContext loads the certificate/key synchronously, so managed
                # temporary files may be removed immediately afterward.
                ssl_context = httpx.create_ssl_context()
                ssl_context.load_cert_chain(
                    certfile=cert_path,
                    keyfile=key_path if key_path else None,
                )

            logger.bind(ctx=ctx).info("Created SSL context for ADP mTLS")
            return ssl_context

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error creating ADP SSL context: {str(e)}")
            return None
