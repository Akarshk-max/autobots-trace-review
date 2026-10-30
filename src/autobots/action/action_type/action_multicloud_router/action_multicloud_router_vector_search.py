from typing import List, Literal, Type

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import ActionABC
from src.autobots.action.action_type.action_multicloud_router.action_multicloud_router import (
    ActionMulticloudRouter,
)
from src.autobots.action.action_type.action_multicloud_router.action_multicloud_router_model import (
    RouterConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.aws.s3 import get_s3
from src.autobots.data_model.context import Context
from src.autobots.datastore.multidatastore import MultiDataStore
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class RouterSearchConfig(RouterConfig):
    datastore_ids: List[str] = []
    top_k: int = 10
    openai_provider: Literal["azure", "openai"] = "azure"


class ActionMulticloudRouterSearch(
    ActionABC[RouterSearchConfig, RouterSearchConfig, RouterSearchConfig, DataBlock, DataBlocks]
):
    type = ActionType.multicloud_router_search
    image_formats = ["jpeg", "jpg", "png", "gif", "webp"]

    # def __init__(self, action_config: RouterSearchConfig, user: UserORM | None = None):
    #     super().__init__(action_config=action_config, user=user)

    @staticmethod
    def get_config_create_type() -> Type[RouterSearchConfig]:
        return RouterSearchConfig

    @staticmethod
    def get_config_update_type() -> Type[RouterSearchConfig]:
        return RouterSearchConfig

    @staticmethod
    def get_config_type() -> Type[RouterSearchConfig]:
        return RouterSearchConfig

    @staticmethod
    def get_input_type() -> Type[DataBlock]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[DataBlocks]:
        return DataBlocks

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        for datastore_id in self.action_config.datastore_ids:
            datastore = MultiDataStore(
                s3=get_s3(), openai_provider=self.action_config.openai_provider
            ).hydrate(datastore_id=datastore_id)

            search_results = await datastore.search(
                action_input.text, top_k=self.action_config.top_k
            )
            for result in search_results:
                action_input.text += "\n" + result.data

        action = ActionMulticloudRouter(
            action_config=self.action_config, user=self.user, db=self.db
        )
        data_blocks: DataBlocks = await action.run_action(ctx=ctx, app_state=app_state, action_input=action_input)
        return data_blocks

    async def run_tool(
        self, action_config: RouterSearchConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        pass
