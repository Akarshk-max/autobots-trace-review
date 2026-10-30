import json
from typing import List, Type

from loguru import logger
from pydantic import Field

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
from src.autobots.conn.perplexity.sonar.chat_model import PerplexityChatReq, PerplexityMessageParam
from src.autobots.conn.perplexity.sonar.perplexity_chat import PerplexityChat
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class PerplexitySonarActionConfig(PerplexityChatReq):
    system_prompt: str | None = Field(
        default=None,
        description="Optional system prompt to prepend if no system message is already present.",
    )
    include_action_input_as_user_message: bool = Field(
        default=True,
        description="When true, run_action appends action_input as the last user message.",
    )


class ActionText2TextPerplexitySonar(
    ActionABC[
        PerplexitySonarActionConfig,
        PerplexitySonarActionConfig,
        PerplexitySonarActionConfig,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.text2text_perplexity_sonar

    @staticmethod
    def get_description() -> str:
        return """
Description:
- Send a chat-style request to Perplexity Sonar and return the final answer as a DataBlocks object.
- Supports normal Sonar options such as model, search controls, streaming, citations, images, related questions, and structured outputs.
- In run_action, the incoming DataBlock is converted into a user message and appended to the configured messages.
- In run_tool, only the explicit action_config is used.

When to use this tool:
- Use when you want Perplexity Sonar as an LLM + web-grounded reasoning tool.
- Use when the upstream agent provides a prompt or a structured DataBlock that should become the next user message.
- Use when you want one final answer block plus URLs collected from citations/search results/media.

Input config:
{
  "model": "sonar-pro",
  "messages": [],
  "system_prompt": "You are a helpful research assistant.",
  "stream": false,
  "include_action_input_as_user_message": true
}

Input:
- DataBlock:
  {
    "text": "...",
    "urls": [...]
  }

Output:
- DataBlocks:
  {
    "blocks": [
      {
        "text": "<final answer and optional source summary>",
        "urls": ["<citation/search/media urls>"]
      }
    ]
  }
"""

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return PerplexitySonarActionConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return PerplexitySonarActionConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return PerplexitySonarActionConfig

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
        curr_config: PerplexitySonarActionConfig,
        prev_results: List[ActionResult] | None = None,
    ) -> PerplexitySonarActionConfig:
        return curr_config

    @staticmethod
    def _dedupe_urls(urls: List[str]) -> List[str]:
        out: List[str] = []
        seen: set[str] = set()
        for url in urls:
            if not url:
                continue
            if url in seen:
                continue
            seen.add(url)
            out.append(url)
        return out

    @staticmethod
    def _datablock_to_user_text(action_input: DataBlock) -> str:
        if getattr(action_input, "text", None):
            text = str(action_input.text).strip()
            if text:
                return text

        payload = action_input.model_dump(exclude_none=True)
        if payload:
            return json.dumps(payload, ensure_ascii=False, indent=2)

        return ""

    @classmethod
    def _build_chat_req(
        cls,
        *,
        action_config: PerplexitySonarActionConfig,
        action_input: DataBlock | None = None,
    ) -> PerplexityChatReq:
        cfg = action_config.model_copy(deep=True)
        messages = list(cfg.messages or [])

        if cfg.system_prompt and not any(message.role == "system" for message in messages):
            messages.insert(0, PerplexityMessageParam(role="system", content=cfg.system_prompt))

        if action_input is not None and cfg.include_action_input_as_user_message:
            user_text = cls._datablock_to_user_text(action_input)
            if user_text:
                messages.append(PerplexityMessageParam(role="user", content=user_text))

        payload = cfg.model_dump(exclude_none=True)
        payload.pop("system_prompt", None)
        payload.pop("include_action_input_as_user_message", None)
        payload["messages"] = [message.model_dump(exclude_none=True) for message in messages]
        return PerplexityChatReq.model_validate(payload)

    @classmethod
    def _result_to_datablocks(cls, result) -> DataBlocks:
        urls: List[str] = []
        urls.extend(result.citations or [])
        urls.extend([item.url for item in (result.search_results or []) if getattr(item, "url", None)])
        urls.extend([item.image_url for item in (result.images or []) if getattr(item, "image_url", None)])
        urls.extend([item.origin_url for item in (result.images or []) if getattr(item, "origin_url", None)])
        urls.extend([item.url for item in (result.videos or []) if getattr(item, "url", None)])
        urls = cls._dedupe_urls(urls)

        text_parts: List[str] = []
        if result.text:
            text_parts.append(result.text.strip())

        if result.related_questions:
            text_parts.append(
                "Related questions:\n" + "\n".join(f"- {question}" for question in result.related_questions)
            )

        if result.search_results:
            source_lines: List[str] = []
            for item in result.search_results[:10]:
                title = item.title or item.url or "Untitled source"
                extra = []
                if item.date:
                    extra.append(f"date={item.date}")
                if item.last_updated:
                    extra.append(f"last_updated={item.last_updated}")
                suffix = f" ({', '.join(extra)})" if extra else ""
                source_lines.append(f"- {title}{suffix}")
            text_parts.append("Source summary:\n" + "\n".join(source_lines))

        final_text = "\n\n".join(part for part in text_parts if part).strip()
        if not final_text:
            final_text = "Perplexity returned no text."

        return DataBlocks(blocks=[DataBlock(text=final_text, urls=urls)])

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        client = None
        try:
            client = await AppConn.get_perplexity(ctx=ctx, db=self.db, user=self.user)
            if isinstance(client, Exception):
                raise client

            chat_req = self._build_chat_req(action_config=self.action_config, action_input=action_input)

            logger.bind(
                ctx=ctx,
                model=chat_req.model,
                stream=chat_req.stream,
                messages_count=len(chat_req.messages),
            ).info("Running Perplexity Sonar action")

            perplexity_chat = PerplexityChat(client)
            result = await perplexity_chat.chat(
                ctx=ctx, chat_req=chat_req, notify=notify, app_state=app_state
            )
            return self._result_to_datablocks(result)
        except Exception as e:
            logger.bind(ctx=ctx).exception("Error in Perplexity Sonar run_action: {}", str(e))
            return e
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    logger.bind(ctx=ctx).debug("Failed to close Perplexity client")

    async def run_tool(
        self,
        action_config: PerplexitySonarActionConfig,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        client = None
        try:
            client = await AppConn.get_perplexity(ctx=ctx, db=self.db, user=self.user)
            if isinstance(client, Exception):
                raise client

            chat_req = self._build_chat_req(action_config=action_config, action_input=None)

            logger.bind(
                ctx=ctx,
                model=chat_req.model,
                stream=chat_req.stream,
                messages_count=len(chat_req.messages),
            ).info("Running Perplexity Sonar tool")

            perplexity_chat = PerplexityChat(client)
            result = await perplexity_chat.chat(
                ctx=ctx, chat_req=chat_req, notify=notify, app_state=app_state
            )
            return self._result_to_datablocks(result)
        except Exception as e:
            logger.bind(ctx=ctx).exception("Error in Perplexity Sonar run_tool: {}", str(e))
            return e
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    logger.bind(ctx=ctx).debug("Failed to close Perplexity client")