from typing import Dict, Optional, Type

from loguru import logger
from pydantic import BaseModel, HttpUrl

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.conn.only_auth.spotify.spotify_client import (
    Spotify,
    SpotifyAccessToken,
    SpotifyAccessTokenRq,
)
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import OAuthModel
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class SpotifyCreds(BaseModel):
    client_id: str
    client_secret: str


class SpotifyAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.spotify

    @staticmethod
    async def get_api_domain(app_creds: SpotifyCreds | None):
        return "https://api.spotify.com"

    @staticmethod
    def get_creds_model_type() -> Type[SpotifyCreds]:
        return SpotifyCreds

    @staticmethod
    def get_auth_model_type() -> Type[OAuthModel]:
        return OAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: SpotifyCreds) -> OAuthModel | Exception:
        try:
            token_rq = SpotifyAccessTokenRq(
                client_id=app_creds.client_id,
                client_secret=app_creds.client_secret,
            )

            access_token = await Spotify.access_token(ctx=ctx, rq=token_rq)

            match access_token:
                case SpotifyAccessToken():
                    res = OAuthModel(
                        access_token=access_token.access_token,
                        token_type=access_token.token_type,
                        expires_in=access_token.expires_in,
                        created_at=access_token.created_at,
                        refresh_token=None,
                        code=None,
                    )
                    logger.bind(ctx=ctx).info("Successfully created Spotify auth")
                    return res
                case Exception():
                    logger.bind(ctx=ctx).error(
                        f"Failed to create Spotify auth: {str(access_token)}"
                    )
                    return access_token
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error creating Spotify auth: {str(e)}")
            return e

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: SpotifyCreds | None, app_auth: OAuthModel | None
    ) -> OAuthModel | Exception:
        if not app_creds:
            return Exception("Spotify credentials are required for token refresh")

        logger.bind(ctx=ctx).info("Refreshing Spotify token by getting new access token")

        return await SpotifyAuth.create_auth(ctx, app_creds)

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
        ).info("is_refresh_required for Spotify")

        return is_refresh

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: OAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.access_token}"}

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: SpotifyCreds, app_auth: OAuthModel, url: str
    ) -> str:
        logger.bind(ctx=ctx, original_url=url).info("Updating Spotify URL")
        try:
            http_url = HttpUrl(url)
            spotify_base_url = await SpotifyAuth.get_api_domain(app_creds)

            if spotify_base_url:
                updated_url = url.replace(
                    f"{http_url.scheme}://{http_url.host}", spotify_base_url.rstrip("/")
                )
                logger.bind(ctx=ctx, updated_url=updated_url).info("Updated Spotify URL")
                return updated_url
            else:
                logger.bind(ctx=ctx).warning("No Spotify base URL found")
                return url

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error updating Spotify URL: {str(e)}")
            return url
