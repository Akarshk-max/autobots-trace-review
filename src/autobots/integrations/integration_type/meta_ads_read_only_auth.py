from src.autobots import SettingsProvider
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.meta_auth import MetaAuth


class MetaAdsReadOnlyAuth(MetaAuth):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.meta_ads_read_only

    @staticmethod
    def oauth_client_id() -> str:
        return SettingsProvider.sget().META_APP_ID

    @staticmethod
    def oauth_scopes() -> str:
        return SettingsProvider.sget().META_ADS_READ_ONLY_SCOPES

    @staticmethod
    def oauth_callback_path() -> str:
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}/meta-ads-read-only/oauth-callback"
