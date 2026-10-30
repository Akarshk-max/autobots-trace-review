from typing import List, Type

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
from src.autobots.conn.deepseek.chat.deepseek_chat import DeepSeekChat
from src.autobots.conn.deepseek.chat.deepseek_common import DeepSeekCommon
from src.autobots.conn.deepseek.chat.deepseek_models import DeepSeekMessage, DeepSeekRole
from src.autobots.conn.deepseek.chat.deepseek_user_config import (
    DeepSeekUserConfig,
    compile_deepseek_plan,
)
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.tool_factory import ToolFactory
from src.autobots.llm.tools.tools_map import TOOLS_MAP
from src.autobots.data_model.app_state import AppState

class DeepSeekChatConfig(DeepSeekUserConfig):
    """Action config = DeepSeek user config"""

    pass


class ActionText2TextLlmChatDeepSeek(
    ActionABC[DeepSeekChatConfig, DeepSeekChatConfig, DeepSeekChatConfig, DataBlock, DataBlocks]
):
    type = ActionType.text2text_llm_chat_deepseek

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return DeepSeekChatConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return DeepSeekChatConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return DeepSeekChatConfig

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
        curr_config: DeepSeekChatConfig,
        prev_results: List[ActionResult] | None = None,
    ) -> DeepSeekChatConfig:
        if not prev_results:
            return curr_config

        msgs = list(curr_config.messages or [])

        for prev in prev_results:
            try:
                out: DataBlocks = ActionText2TextLlmChatDeepSeek.get_output_type().model_validate(
                    prev.output
                )
                for b in out.blocks:
                    if b.text and b.text.strip():
                        msgs.append(DeepSeekMessage(role=DeepSeekRole.assistant, content=b.text))
            except Exception:
                logger.bind(ctx=ctx).warning("Failed to merge prev_results into DeepSeek messages")

        return curr_config.model_copy(update={"messages": msgs})

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify=None,
    ) -> DataBlocks | Exception:
        data_blocks = DataBlocks(blocks=[])

        try:
            deepseek_conn = await AppConn.get_deepseek(ctx=ctx, db=self.db, user=self.user)
            common = DeepSeekCommon(conn=deepseek_conn)

            user_cfg = DeepSeekUserConfig(**self.action_config.model_dump(exclude_none=True))
            plan = compile_deepseek_plan(user_cfg, tools_map=TOOLS_MAP)

            req = plan.req

            prompt = ""
            if action_input:
                prompt = action_input.get_llm_prompt(ctx=ctx)

            if prompt:
                req.messages.append(DeepSeekMessage(role=DeepSeekRole.user, content=prompt))

            tool_runner = None
            if plan.tool_names:
                tool_runner = ToolFactory(user=self.user, db=self.db)

            deepseek_chat = DeepSeekChat(common=common)

            out = await deepseek_chat.run(
                ctx=ctx,
                app_state=app_state,
                user=self.user,
                req=req,
                db=self.db,
                notify=notify,
                tool_runner=tool_runner,
                max_rounds=user_cfg.max_rounds,
                default_policy=plan.policies.default,
                policy_overrides=plan.policies.overrides,
                allowed_tools=plan.tool_names,
            )

            if not out or not out.content or not out.content.strip():
                raise HTTPException(
                    status_code=503, detail="No response from DeepSeek, please try again."
                )

            data_blocks.blocks.append(DataBlock(text=out.content))
            return data_blocks

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in DeepSeek run_action: {str(e)}")
            return e
