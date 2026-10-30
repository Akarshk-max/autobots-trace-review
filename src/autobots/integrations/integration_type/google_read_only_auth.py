from loguru import logger

from src.autobots import SettingsProvider
from src.autobots.conn.only_auth.google.google import (
    Google,
    GoogleRefreshedAccessToken,
    GoogleRefreshTokenRq,
)
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import OAuthModel
from src.autobots.integrations.integration_type.google_auth import GoogleAuth


class GoogleReadOnlyAuth(GoogleAuth):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.google_read_only

    @staticmethod
    def oauth_client_id() -> str:
        return SettingsProvider.sget().GOOGLE_READ_ONLY_CLIENT_ID

    @staticmethod
    def oauth_scopes() -> str:
        return SettingsProvider.sget().GOOGLE_READ_ONLY_SCOPES

    @staticmethod
    def oauth_callback_path() -> str:
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}/google-read-only/oauth-callback"

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds, app_auth: OAuthModel | None
    ) -> OAuthModel | Exception:
        token_rq = GoogleRefreshTokenRq(
            client_id=SettingsProvider.sget().GOOGLE_READ_ONLY_CLIENT_ID,
            client_secret=SettingsProvider.sget().GOOGLE_READ_ONLY_CLIENT_SECRET,
            refresh_token=app_auth.refresh_token,
        )
        refreshed_access_token = await Google.refresh_access_token(ctx, token_rq)
        match refreshed_access_token:
            case GoogleRefreshedAccessToken():
                new_auth_model = OAuthModel.model_validate(app_auth)
                new_auth_model.access_token = refreshed_access_token.access_token
                new_auth_model.expires_in = refreshed_access_token.expires_in
                new_auth_model.created_at = Clock.datetime_now()
                logger.bind(ctx=ctx).info("Refreshed token for Google Read Only")
                return new_auth_model
            case Exception():
                logger.bind(ctx=ctx).error("Unable to refresh token for Google Read Only")
                return refreshed_access_token
