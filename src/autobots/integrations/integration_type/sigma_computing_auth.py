from typing import Dict, Optional, Type

from loguru import logger
from pydantic import BaseModel, HttpUrl

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.conn.only_auth.sigma_computing.sigma_computing_client import (
    SigmaComputing,
    SigmaComputingAccessToken,
    SigmaComputingAccessTokenRq,
)
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import OAuthModel
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class SigmaComputingCreds(BaseModel):
    base_url: str  # e.g., "https://aws-api.sigmacomputing.com"
    client_id: str
    client_secret: str


class SigmaComputingAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.sigma_computing

    @staticmethod
    async def get_api_domain(app_creds: SigmaComputingCreds | None):
        return app_creds.base_url if app_creds else None

    @staticmethod
    def get_creds_model_type() -> Type[SigmaComputingCreds]:
        return SigmaComputingCreds

    @staticmethod
    def get_auth_model_type() -> Type[OAuthModel]:
        return OAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: SigmaComputingCreds) -> OAuthModel | Exception:
        token_rq = SigmaComputingAccessTokenRq(
            client_id=app_creds.client_id,
            client_secret=app_creds.client_secret,
            base_url=app_creds.base_url,
        )

        access_token = await SigmaComputing.access_token(ctx=ctx, rq=token_rq)
        match access_token:
            case SigmaComputingAccessToken():
                res = OAuthModel(
                    access_token=access_token.access_token,
                    token_type=access_token.token_type,
                    expires_in=access_token.expires_in,
                    created_at=access_token.created_at,
                    refresh_token=access_token.refresh_token,
                    code=None,
                )
                return res
            case Exception():
                return access_token

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: SigmaComputingCreds | None, app_auth: OAuthModel | None
    ) -> OAuthModel | Exception:
        if not app_creds:
            return Exception("Sigma Computing credentials are required for token refresh")

        logger.bind(ctx=ctx).info("Refreshing Sigma Computing token by getting new access token")
        return await SigmaComputingAuth.create_auth(ctx, app_creds)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: OAuthModel) -> bool:
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
            curr_time=str(curr_time),
            auth_creation_time=str(auth_creation_time),
        ).info("is_refresh_required for Sigma Computing")

        return is_refresh

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: OAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.access_token}"}

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: SigmaComputingCreds, app_auth: OAuthModel, url: str
    ) -> str:
        logger.bind(ctx=ctx, original_url=url).info("Updating Sigma Computing URL")
        try:
            http_url = HttpUrl(url)
            sigma_base_url = await SigmaComputingAuth.get_api_domain(app_creds)

            if sigma_base_url:
                updated_url = url.replace(
                    f"{http_url.scheme}://{http_url.host}", sigma_base_url.rstrip("/")
                )
                logger.bind(ctx=ctx, updated_url=updated_url).info("Updated Sigma Computing URL")
                return updated_url
            else:
                logger.bind(ctx=ctx).warning("No Sigma Computing base URL found in credentials")
                return url

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error updating Sigma Computing URL: {str(e)}")
            return url
