from typing import List, Type

from anthropic.types import TextBlockParam
from google.genai import types
from google.genai.types import Part
from loguru import logger
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
)

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import ActionABC
from src.autobots.action.action_type.action_multicloud_router.action_multicloud_router_model import (
    AWS_LLM,
    GCP_LLM,
    Azure_LLM,
    RouterConfig,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_claude import (
    ActionText2TextLlmChatclaude,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_gemini import (
    ActionText2TextLlmChatGemini,
    GeminiChatConfig,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    ActionText2TextLlmChatOpenai,
    LlmChatOpenaiConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.claude.chat_model import ChatReqClaude, ClaudeMessageParam
from src.autobots.conn.gemini.gemini_model import ChatConfigGemini
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class ActionMulticloudRouter(
    ActionABC[RouterConfig, RouterConfig, RouterConfig, DataBlock, DataBlocks]
):
    type = ActionType.multicloud_router
    image_formats = ["jpeg", "jpg", "png", "gif", "webp"]

    @staticmethod
    def get_config_create_type() -> Type[RouterConfig]:
        return RouterConfig

    @staticmethod
    def get_config_update_type() -> Type[RouterConfig]:
        return RouterConfig

    @staticmethod
    def get_config_type() -> Type[RouterConfig]:
        return RouterConfig

    @staticmethod
    def get_input_type() -> Type[DataBlock]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[DataBlocks]:
        return DataBlocks

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            logger.bind(ctx=ctx).info(f"Cloud provider: {self.action_config.engine.cloud}")
            match self.action_config.engine:
                case AWS_LLM():
                    return await self.run_action_for_aws_llm(ctx=ctx, app_state=app_state, action_input=action_input)
                case Azure_LLM():
                    return await self.run_action_for_azure_llm(ctx=ctx, app_state=app_state, action_input=action_input)
                case GCP_LLM():
                    return await self.run_action_for_gcp_llm(ctx=ctx, app_state=app_state, action_input=action_input)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in run_action: {str(e)}")
            return e

    async def run_tool(
        self, action_config: RouterConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        pass

    async def run_action_for_aws_llm(self, ctx: Context, app_state: AppState, action_input: DataBlock) -> DataBlocks:
        logger.bind(ctx=ctx).info("Choosing Cloud Service: AWS")
        assert isinstance(self.action_config.engine, AWS_LLM)
        msgs: List[ClaudeMessageParam] = []
        system_msg_content = TextBlockParam(type="text", text="")
        for instruction in self.action_config.instructions:
            match instruction.role:
                case "system":
                    data_block = DataBlock.model_validate(instruction.content)
                    system_msg_content["text"] += f"\n{data_block.text}"
                case "user":
                    content = []
                    if instruction.content.text:
                        content.append(TextBlockParam(type="text", text=instruction.content.text))
                    msgs.append(ClaudeMessageParam(role="user", content=content))
                case "assistant":
                    content = []
                    if instruction.content.text:
                        content.append(TextBlockParam(type="text", text=instruction.content.text))
                    msgs.append(ClaudeMessageParam(role="assistant", content=content))

        if not system_msg_content.get("text"):
            system_msg_content["text"] = "You are a helpful autonomous AI assistant"
        msgs += self.action_config.thread
        action_config = ChatReqClaude(
            model=self.action_config.engine.model,
            system=[system_msg_content],
            tools=self.action_config.tools,
            messages=msgs,
        )
        action = ActionText2TextLlmChatclaude(
            action_config=action_config, user=self.user, db=self.db
        )
        logger.bind(
            ctx=ctx,
            action_config=action_config.model_dump(exclude_none=True),
            action_input=action_input,
        ).info("Starting Claude action")
        data_blocks: DataBlocks = await action.run_action(ctx=ctx, app_state=app_state, action_input=action_input)
        logger.bind(ctx=ctx, action_output=data_blocks).info("Completed Claude action")
        self.action_config.thread += action_config.messages[len(msgs) :]
        return data_blocks

    async def run_action_for_azure_llm(self, ctx: Context, app_state: AppState, action_input: DataBlock) -> DataBlocks:
        logger.bind(ctx=ctx).info("Choosing Cloud Service: Azure")
        assert isinstance(self.action_config.engine, Azure_LLM)
        msgs: List[ChatCompletionMessageParam] = []
        for instruction in self.action_config.instructions:
            match instruction.role:
                case "system":
                    content = []
                    if instruction.content.text:
                        content.append(
                            ChatCompletionContentPartTextParam(
                                type="text", text=instruction.content.text
                            )
                        )
                    msgs.append(ChatCompletionSystemMessageParam(role="system", content=content))
                case "user":
                    content = []
                    if instruction.content.text:
                        content.append(
                            ChatCompletionContentPartTextParam(
                                type="text", text=instruction.content.text
                            )
                        )
                    msgs.append(ChatCompletionUserMessageParam(role="user", content=content))
                case "assistant":
                    content = []
                    if instruction.content.text:
                        content.append(
                            ChatCompletionContentPartTextParam(
                                type="text", text=instruction.content.text
                            )
                        )
                    msgs.append(
                        ChatCompletionAssistantMessageParam(role="assistant", content=content)
                    )
        msgs += self.action_config.thread
        action_config = LlmChatOpenaiConfig(
            provider="azure",
            model=self.action_config.engine.model,
            tools=self.action_config.tools,
            messages=msgs,
        )
        action = ActionText2TextLlmChatOpenai(
            action_config=action_config, user=self.user, db=self.db
        )
        logger.bind(
            ctx=ctx,
            action_config=action_config.model_dump(exclude_none=True),
            action_input=action_input,
        ).info("Starting Openai action")
        data_blocks: DataBlocks = await action.run_action(ctx=ctx, app_state=app_state, action_input=action_input)
        logger.bind(ctx=ctx, action_output=data_blocks).info("Completed Openai action")
        self.action_config.thread += action_config.messages[len(msgs) :]
        return data_blocks

    async def run_action_for_gcp_llm(self, ctx: Context, app_state: AppState, action_input: DataBlock) -> DataBlocks:
        logger.bind(ctx=ctx).info("Choosing Cloud Service: GCP")
        assert isinstance(self.action_config.engine, GCP_LLM)
        msgs: List[types.Content] = []
        for instruction in self.action_config.instructions:
            parts: List[Part] = []
            if instruction.content.text:
                parts.append(Part(text=instruction.content.text))
            if instruction.content.urls:
                url_parts = await ActionText2TextLlmChatGemini.file_urls_to_file_data_parts(
                    ctx=ctx, user=self.user, db=self.db, urls=instruction.content.urls
                )
                parts += url_parts
            llm_message = types.Content(role=instruction.role, parts=parts)
            msgs.append(llm_message)

        for thread_message in self.action_config.thread:
            msgs.append(types.Content.model_validate(thread_message))

        gemini_config = ChatConfigGemini(
            tools=self.action_config.tools,
        )
        action_config = GeminiChatConfig(
            model=self.action_config.engine.model, contents=msgs, config=gemini_config
        )
        action = ActionText2TextLlmChatGemini(
            action_config=action_config, user=self.user, db=self.db
        )
        logger.bind(
            ctx=ctx,
            action_config=action_config.model_dump(exclude_none=True),
            action_input=action_input,
        ).info("Starting Gemini action")
        data_blocks: DataBlocks = await action.run_action(ctx=ctx, app_state=app_state, action_input=action_input)
        logger.bind(ctx=ctx, action_output=data_blocks).info("Completed Gemini action")
        self.action_config.thread += action_config.messages[len(msgs) :]
        return data_blocks
