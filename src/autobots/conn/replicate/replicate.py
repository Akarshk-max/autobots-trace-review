import replicate

from src.autobots.conn.replicate.yan_ops.yan_ops_face_swap import YanOpsFaceSwap
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


class Replicate:
    def __init__(self, api_key: str):
        self.client = replicate.client.Client(api_token=api_key)
        self.yan_ops_face_swap = YanOpsFaceSwap(self.client)

    async def list_collections_namespace(self):
        collections = [
            collection
            for page in replicate.paginate(self.client.collections.list)
            for collection in page
        ]
        return collections

    async def list_models(self):
        models = [model for page in replicate.paginate(self.client.models.list) for model in page]
        return models


def get_replicate_client(
    ctx: Context,
    user: UserORM,
    api_key: str,
    notify: Notify,
    settings: Settings = SettingsProvider.sget(),
) -> Replicate:
    return Replicate(api_key=api_key)
