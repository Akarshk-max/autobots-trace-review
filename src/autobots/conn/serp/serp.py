from src.autobots.conn.serp.serp_google_search import SerpGoogleSearch
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


class Serp:
    def __init__(self, ctx: Context, user: UserORM, api_key: str, notify: Notify = None):
        self.api_key = api_key
        self.serp_google_search = SerpGoogleSearch(api_key)


def get_serp_client(
    ctx: Context,
    user: UserORM,
    api_key: str,
    notify: Notify = None,
    settings: Settings = SettingsProvider.sget(),
) -> Serp:
    return Serp(ctx=ctx, user=user, api_key=api_key, notify=notify)
