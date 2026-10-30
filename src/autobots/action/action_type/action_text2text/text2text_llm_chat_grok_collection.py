import mimetypes
from typing import List, Tuple, Type

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
from src.autobots.conn.grok.grok_chat.grok_collection_chat import GrokCollectionChat
from src.autobots.conn.grok.grok_chat.grok_collection_chat_config import (
    GrokCollectionUserConfig,
    compile_grok_collection_plan,
)
from src.autobots.conn.grok.grok_chat.grok_models import (
    DEFAULT_MODEL_REGISTRY,
    GrokMessage,
    GrokRole,
)
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.tool_factory import ToolFactory
from src.autobots.data_model.app_state import AppState

class GrokCollectionChatConfig(GrokCollectionUserConfig):
    pass


class ActionText2TextLlmChatGrokCollection(
    ActionABC[
        GrokCollectionChatConfig,
        GrokCollectionChatConfig,
        GrokCollectionChatConfig,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.text2text_llm_chat_grok_collections

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return GrokCollectionChatConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return GrokCollectionChatConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return GrokCollectionChatConfig

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
        curr_config: GrokCollectionChatConfig,
        prev_results: List[ActionResult] | None = None,
    ) -> GrokCollectionChatConfig:
        if not prev_results:
            return curr_config

        msgs: List[GrokMessage] = list(curr_config.messages or [])
        for prev in prev_results:
            try:
                out: DataBlocks = (
                    ActionText2TextLlmChatGrokCollection.get_output_type().model_validate(
                        prev.output
                    )
                )
                for b in out.blocks:
                    if b.text and b.text.strip():
                        msgs.append(GrokMessage(role=GrokRole.assistant, content=b.text))
            except Exception:
                logger.bind(ctx=ctx).warning(
                    "Failed to merge prev_results into Grok collection messages"
                )

        return curr_config.model_copy(update={"messages": msgs})

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify=None
    ) -> DataBlocks | Exception:
        data_blocks = DataBlocks(blocks=[])
        try:
            grok_conn = await AppConn.get_grok(ctx=ctx, db=self.db, user=self.user)
            client = grok_conn.client

            # Build user config
            user_cfg = GrokCollectionUserConfig(**self.action_config.model_dump(exclude_none=True))

            # Append user prompt as last message BEFORE compile (so retrieval+vision checks are correct)
            prompt = ""
            if action_input:
                prompt = action_input.get_llm_prompt(ctx=ctx) or ""

            if prompt.strip():
                user_cfg.messages = list(user_cfg.messages or [])
                user_cfg.messages.append(GrokMessage(role=GrokRole.user, content=prompt))

            # Attach URLs (images vs docs). Also feed docs into collection upload_urls if enabled.
            urls = list(getattr(action_input, "urls", None) or [])
            if urls:
                img_urls, file_urls = self._split_image_vs_file_urls(urls)

                # Chat attachments
                user_cfg.attachments.image_urls = (
                    list(user_cfg.attachments.image_urls or []) + img_urls
                )
                user_cfg.attachments.file_urls = (
                    list(user_cfg.attachments.file_urls or []) + file_urls
                )

                # Collection ingestion
                if (
                    user_cfg.collection.enabled
                    and user_cfg.collection.upload_from_action_input_urls
                ):
                    user_cfg.collection.upload_urls = (
                        list(user_cfg.collection.upload_urls or []) + file_urls
                    )

            tool_runner = None
            if user_cfg.tools:
                tool_runner = ToolFactory(user=self.user, db=self.db)

            # Compile plan (keeps policies aligned with tools by default)
            plan = compile_grok_collection_plan(user_cfg, DEFAULT_MODEL_REGISTRY)
            req = plan.chat_plan.req

            # Run collection-augmented chat
            collection_chat = GrokCollectionChat(
                client=client,
                timeout_sec=req.timeout_sec or self.action_config.timeout_sec or 420.0,
                model_registry=DEFAULT_MODEL_REGISTRY,
            )

            out = await collection_chat.run(
                ctx=ctx,
                app_state=app_state,
                user=self.user,
                db=self.db,
                req=req,
                plan=plan,
                notify=notify,
                tool_runner=tool_runner,
                max_rounds=user_cfg.max_rounds,
            )

            if not out or not out.content or not out.content.strip():
                raise HTTPException(
                    status_code=503, detail="No response from Grok, please try again."
                )

            data_blocks.blocks.append(DataBlock(text=out.content))
            return data_blocks

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in GrokCollection run_action: {str(e)}")
            return e

    @staticmethod
    def _split_image_vs_file_urls(urls: List[str]) -> Tuple[List[str], List[str]]:
        img_urls: List[str] = []
        file_urls: List[str] = []
        for u in urls:
            mt, _ = mimetypes.guess_type(u)
            if mt and mt.startswith("image/"):
                img_urls.append(u)
            else:
                file_urls.append(u)
        return img_urls, file_urls
