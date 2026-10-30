from typing import Literal, Type

from loguru import logger
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import ActionABC
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_azure_openai import (
    ActionText2TextLlmChatAzureOpenAI,
    LlmChatAzureOpenAIBYOKConfig,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_claude import (
    ActionText2TextLlmChatclaude,
    ClaudeChatConfig,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_gemini import (
    ActionText2TextLlmChatGemini,
    GeminiChatConfig,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_grok import (
    ActionText2TextLlmChatGrok,
    GrokChatConfig,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    ActionText2TextLlmChatOpenai,
    LlmChatOpenaiConfig,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openrouter import (
    ActionText2TextLlmChatOpenRouter,
    OpenRouterChatConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify


class LlmConfig(BaseModel):
    """Configuration for LLM action."""

    provider: Literal["openai", "azure_byok", "gemini", "claude", "grok", "openrouter"]
    model: str
    prompt: str | None = None


class ActionLlm(ActionABC[LlmConfig, LlmConfig, LlmConfig, DataBlock, DataBlocks]):
    type = ActionType.action_llm

    @staticmethod
    def get_config_create_type() -> Type[LlmConfig]:
        return LlmConfig

    @staticmethod
    def get_config_update_type() -> Type[LlmConfig]:
        return LlmConfig

    @staticmethod
    def get_config_type() -> Type[LlmConfig]:
        return LlmConfig

    @staticmethod
    def get_input_type() -> Type[DataBlock]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[DataBlocks]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
Description:
- Generic LLM action that routes to specific LLM providers (OpenAI, Azure OpenAI BYOK, Gemini, Claude, Grok, OpenRouter).
- Allows you to use any LLM provider with a unified interface.
- Automatically creates the appropriate provider-specific configuration.

When to use this tool:
- Use when you need to call an LLM without knowing which provider is available.
- Use when you want a simple, unified interface across multiple LLM providers.
- Use when you want to switch between LLM providers easily.

Input (DataBlock):
- text: The prompt/text to send to the LLM

Config:
- provider: 'openai', 'azure_byok', 'gemini', 'claude', 'grok', or 'openrouter'
- model: The model name (e.g., 'gpt-4', 'gemini-2.5-pro', 'claude-3-7-sonnet-20250219', 'grok-4.6', 'deepseek/deepseek-v3.2')
- prompt: Optional base prompt (will be combined with input text)

Output (DataBlocks):
- DataBlocks containing the LLM's response text
"""

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            if notify:
                notify(ctx, f"Running LLM action with provider: {self.action_config.provider}")

            logger.bind(
                ctx=ctx,
                provider=self.action_config.provider,
                model=self.action_config.model,
            ).info("Starting LLM action")

            prompt_parts = []
            if self.action_config.prompt:
                prompt_parts.append(self.action_config.prompt)
            if action_input and action_input.text:
                if action_input.text != self.action_config.prompt:
                    prompt_parts.append(action_input.text)

            if not prompt_parts:
                raise ValueError("Either config.prompt or action_input.text must be provided")

            final_prompt = "\n".join(prompt_parts)
            input_block = DataBlock(text=final_prompt)

            logger.bind(ctx=ctx, final_prompt_length=len(final_prompt)).info(
                "Combined prompt created"
            )

            match self.action_config.provider:
                case "openai":
                    config = LlmChatOpenaiConfig(
                        provider="openai",
                        model=self.action_config.model,
                        messages=[],
                    )
                    action = ActionText2TextLlmChatOpenai(
                        action_config=config, db=self.db, user=self.user
                    )
                    output: DataBlocks = await action.run_action(
                        ctx=ctx, app_state=app_state, action_input=input_block, notify=notify
                    )

                case "azure_byok":
                    config = LlmChatAzureOpenAIBYOKConfig(
                        provider="azure_byok",
                        model=self.action_config.model,
                        messages=[],
                    )
                    action = ActionText2TextLlmChatAzureOpenAI(
                        action_config=config, db=self.db, user=self.user
                    )
                    output: DataBlocks = await action.run_action(
                        ctx=ctx, app_state=app_state, action_input=input_block, notify=notify
                    )

                case "gemini":
                    config = GeminiChatConfig(
                        model=self.action_config.model,
                        contents=[],
                    )
                    action = ActionText2TextLlmChatGemini(
                        action_config=config, db=self.db, user=self.user
                    )
                    output: DataBlocks = await action.run_action(
                        ctx=ctx, app_state=app_state, action_input=input_block, notify=notify
                    )

                case "claude":
                    config = ClaudeChatConfig(
                        model=self.action_config.model,
                        messages=[],
                        max_tokens=8192,
                    )
                    action = ActionText2TextLlmChatclaude(
                        action_config=config, db=self.db, user=self.user
                    )
                    output: DataBlocks = await action.run_action(
                        ctx=ctx, app_state=app_state, action_input=input_block, notify=notify
                    )

                case "grok":
                    config = GrokChatConfig(
                        model=self.action_config.model,
                        messages=[],
                    )
                    action = ActionText2TextLlmChatGrok(
                        action_config=config, db=self.db, user=self.user
                    )
                    output: DataBlocks = await action.run_action(
                        ctx=ctx, app_state=app_state, action_input=input_block, notify=notify
                    )

                case "openrouter":
                    config = OpenRouterChatConfig(
                        model=self.action_config.model,
                        messages=[],
                        tools=[],
                    )
                    action = ActionText2TextLlmChatOpenRouter(
                        action_config=config, db=self.db, user=self.user
                    )
                    output: DataBlocks = await action.run_action(
                        ctx=ctx, app_state=app_state, action_input=input_block, notify=notify
                    )

                case _:
                    raise ValueError(f"Unknown provider: {self.action_config.provider}")

            logger.bind(
                ctx=ctx,
                provider=self.action_config.provider,
                output_blocks_count=len(output.blocks),
            ).info("LLM action completed successfully")

            if notify:
                notify(ctx, f"LLM action completed with {len(output.blocks)} response(s)")

            return output

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in LLM action: {str(e)}")
            return e

    async def run_tool(
        self, action_config: LlmConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlock | Exception:
        try:
            logger.bind(ctx=ctx).info("Running LLM tool")

            if not action_config.prompt:
                raise ValueError("prompt is required for run_tool")

            self.action_config = action_config

            result = await self.run_action(ctx, app_state, DataBlock(text=action_config.prompt), notify)

            if isinstance(result, Exception):
                return result

            if len(result.blocks) > 0:
                return result.blocks[0]
            else:
                raise ValueError("LLM action returned no output blocks")

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in LLM tool: {str(e)}")
            return e
