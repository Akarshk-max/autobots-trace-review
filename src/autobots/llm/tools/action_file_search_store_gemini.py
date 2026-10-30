import json
from typing import Type

from loguru import logger

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import (
    ActionABC,
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
    ActionInputType,
    ActionOutputType,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.gemini.file_search_store.file_search_store_client import (
    GeminiFileSearchStoreClient,
)
from src.autobots.conn.gemini.file_search_store.file_search_store_models import (
    GeminiFileSearchStoreReq,
    GeminiFileSearchStoreRes,
)
from src.autobots.conn.gemini.file_search_store.file_search_store_service import (
    GeminiFileSearchStoreService,
)
from src.autobots.conn.gemini.model_registry_gemini import GeminiModelRegistry
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class ActionFileSearchStoreGemini(
    ActionABC[
        GeminiFileSearchStoreReq,
        GeminiFileSearchStoreReq,
        GeminiFileSearchStoreReq,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.action_text2text_file_search_store_gemini

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return GeminiFileSearchStoreReq

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return GeminiFileSearchStoreReq

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return GeminiFileSearchStoreReq

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
Gemini File Search Store management tool.

Use this tool to manage Gemini-backed file search stores.

Supported operations
--------------------
- create_store
- get_store
- list_stores
- ingest_files
- reindex_documents
- delete_documents
- delete_store
- query_store
- stats

Use cases
---------
- create and manage a reusable retrieval store
- ingest files from URLs or existing Gemini file names
- reindex file-search documents
- query a store using Gemini File Search grounding
"""

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        try:
            gemini_conn = await AppConn.get_gemini(ctx=ctx, db=self.db, user=self.user)
            if isinstance(gemini_conn, Exception):
                raise gemini_conn

            service_client = GeminiFileSearchStoreClient(gemini_client=gemini_conn)
            result = await GeminiFileSearchStoreService.execute(
                req=self.action_config,
                client=service_client,
                ctx=ctx,
                user=self.user,
                db=self.db,
                model=GeminiModelRegistry.default_model(),
            )
            return DataBlocks(
                blocks=[
                    DataBlock(
                        text=json.dumps(result.model_dump(exclude_none=True), indent=2, ensure_ascii=False)
                    )
                ]
            )
        except Exception as exc:
            logger.bind(ctx=ctx).exception("Error in ActionFileSearchStoreGemini.run_action: {}", exc)
            return exc

    async def run_tool(
        self,
        action_config: GeminiFileSearchStoreReq,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> GeminiFileSearchStoreRes | Exception:
        try:
            gemini_conn = await AppConn.get_gemini(ctx=ctx, db=self.db, user=self.user)
            if isinstance(gemini_conn, Exception):
                raise gemini_conn

            service_client = GeminiFileSearchStoreClient(gemini_client=gemini_conn)
            return await GeminiFileSearchStoreService.execute(
                req=action_config,
                client=service_client,
                ctx=ctx,
                user=self.user,
                db=self.db,
                model=GeminiModelRegistry.default_model(),
            )
        except Exception as exc:
            logger.bind(ctx=ctx).exception("Error in ActionFileSearchStoreGemini.run_tool: {}", exc)
            return exc
