from typing import Type

from fastapi import HTTPException
from loguru import logger

from src.autobots.action.action.action_doc_model import ActionResult
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
from src.autobots.conn.gemini.chat_model_gemini import GeminiChatConfig, GeminiToolSelection
from src.autobots.conn.gemini.gemini_chat import GeminiChatRuntime
from src.autobots.conn.gemini.model_registry_gemini import GeminiModelRegistry
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.common_default_tools import GEMINI_DEFAULT_TOOLS
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class GeminiProvider(str):
    API = "api"
    VERTEX = "vertex"


class GeminiChatActionConfig(GeminiChatConfig):
    tool_output_text_to_file_char_limit: int | None = 50_000
    gemini_provider: str = GeminiProvider.API


class ActionText2TextLlmChatGemini(
    ActionABC[
        GeminiChatActionConfig,
        GeminiChatActionConfig,
        GeminiChatActionConfig,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.text2text_llm_chat_gemini

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return GeminiChatActionConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return GeminiChatActionConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return GeminiChatActionConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    async def update_config_with_prev_results(
        ctx: Context,
        app_state: AppState,
        curr_config: GeminiChatActionConfig,
        prev_results: list[ActionResult] | None = None,
    ) -> GeminiChatActionConfig:
        return curr_config

    @staticmethod
    def _ensure_default_tool_selection(config: GeminiChatActionConfig) -> None:
        selection = config.tool_selection
        if isinstance(selection, dict):
            selection = GeminiToolSelection.model_validate(selection)

        defaults = list(GEMINI_DEFAULT_TOOLS)
        merged: list[str] = []
        for name in defaults + list(selection.default_tool_names or []):
            if name not in merged:
                merged.append(name)
        selection.default_tool_names = merged
        if not selection.function_calling_mode:
            selection.function_calling_mode = "AUTO"
        config.tool_selection = selection

    @staticmethod
    def _ensure_default_model(config: GeminiChatActionConfig) -> None:
        if not getattr(config, "model", None):
            config.model = GeminiModelRegistry.default_model()

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        try:
            self._ensure_default_model(self.action_config)
            self._ensure_default_tool_selection(self.action_config)

            gemini_conn = await AppConn.get_gemini(
                ctx=ctx,
                db=self.db,
                user=self.user,
                gemini_provider=self.action_config.gemini_provider,
            )
            if isinstance(gemini_conn, Exception):
                raise gemini_conn

            runtime = GeminiChatRuntime(
                user=self.user,
                db=self.db,
                gemini_client=gemini_conn,
            )
            result, updated_config = await runtime.execute(
                ctx=ctx,
                app_state=app_state,
                config=self.action_config,
                action_input=action_input,
                notify=notify,
                output_text_to_file_char_limit=self.action_config.tool_output_text_to_file_char_limit,
            )
            self.action_config = updated_config

            if not result.blocks:
                raise HTTPException(
                    status_code=503,
                    detail="No response from Gemini, please try again.",
                )
            return result
        except Exception as exc:
            logger.bind(ctx=ctx).exception(
                "Error in ActionText2TextLlmChatGemini.run_action: {}", exc
            )
            return exc
