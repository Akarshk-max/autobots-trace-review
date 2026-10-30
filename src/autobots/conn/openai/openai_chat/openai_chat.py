from typing import Any, Dict, List, Literal, Type

from azure.ai.inference.aio import ChatCompletionsClient
from azure.ai.inference.models import ChatCompletions, StreamingChatCompletionsUpdate
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai import AsyncAzureOpenAI, AsyncOpenAI, AsyncStream
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessage,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
)
from tenacity import retry, stop_after_attempt, wait_fixed

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.conn.openai.azure_openai_client_factory import AzureOpenAIModelClientFactory
from src.autobots.conn.openai.openai_chat.chat_model import ChatReq
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.chat_snapshot.provider_snapshots import snapshot_openai_chat
from src.autobots.llm.tools.tool_factory import ToolFactory
from src.autobots.server_sent_events.server_sent_events import Notify, NotifyPayload
from src.autobots.server_sent_events.topics import text_topic
from src.autobots.token_usage.token_usage_service import LlmSurface, TokenUsageService
from src.autobots.user.user_orm_model import UserORM


class OpenaiChat:
    def __init__(
        self,
        provider: Literal["azure", "openai", "azure_byok"],
        client_factory: Type[AzureOpenAIModelClientFactory] | None = None,  # for azure
        openai_client: AsyncOpenAI | None = None,  # for openai/azure_byok
    ):
        assert client_factory or openai_client, (
            "Either client_factory or openai_client must be provided"
        )
        self.provider = provider
        self.client = openai_client
        self.client_factory = client_factory


    def _get_max_loop(self, chat_req: ChatReq) -> int:
        if not chat_req.tools:
            return 20
        for tool in chat_req.tools:
            if isinstance(tool, str) and tool == "run_terminal_command":
                return 50
            if isinstance(tool, dict):
                func = tool.get("function")
                if isinstance(func, dict) and func.get("name") == "run_terminal_command":
                    return 50
            if hasattr(tool, "function") and getattr(tool.function, "name", None) == "run_terminal_command":
                return 50
            if hasattr(tool, "name") and tool.name == "run_terminal_command":
                return 50
        return 20
    
    @retry(stop=stop_after_attempt(3), wait=wait_fixed(3), reraise=True)
    async def chat(
        self,
        chat_req: ChatReq,
        app_state: AppState,
        user: UserORM | None = None,
        ctx: Context | None = None,
        db: AsyncIOMotorDatabase | None = None,
        notify: Notify | None = None,
        tool_output_text_to_file_char_limit: int | None = None,

    ) -> ChatCompletion | AsyncStream[ChatCompletionChunk] | None:
        try:
            logger.bind(ctx=ctx).trace("Starting OpenAI Chat, try: 1")
            # res: ChatCompletion = await self.client.chat.completions.create(**chat_req.model_dump(exclude_none=True))
            res: ChatCompletion | None = None
            if chat_req.stream:
                res = await self.chat_loop_stream(
                    ctx=ctx,
                    app_state=app_state,
                    chat_req=chat_req,
                    user=user,
                    db=db,
                    notify=notify,
                    tool_output_text_to_file_char_limit=tool_output_text_to_file_char_limit,

                )
            else:
                res = await self.chat_loop(
                    ctx=ctx,
                    app_state=app_state,
                    chat_req=chat_req,
                    user=user,
                    db=db,
                    notify=notify,
                    tool_output_text_to_file_char_limit=tool_output_text_to_file_char_limit,

                )
            logger.trace("Completed OpenAI Chat")
            if isinstance(res, AsyncStream):
                return self.yield_chat_chunks(res)
            else:
                is_respnse: bool = False
                if res and res.choices:
                    for choice in res.choices:
                        if choice.message.content:
                            is_respnse = True
                            break
                if not is_respnse:
                    logger.bind(ctx=ctx, chat_res=res).error(
                        "No response from OpenAI, please try again."
                    )
                    raise Exception("No response from OpenAI, please try again.")
                return res
        except Exception as e:
            logger.bind(ctx=ctx, chat_req=chat_req.model_dump(exclude_none=True)).exception(
                f"Error in Openai Chat: {str(e)}"
            )
            raise

    async def chat_loop(
        self,
        ctx: Context,
        app_state: AppState,
        chat_req: ChatReq,
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase | None = None,
        notify: Notify | None = None,
        tool_output_text_to_file_char_limit: int | None = None,

    ) -> (
        AsyncStream[ChatCompletionChunk]
        | ChatCompletions
        | StreamingChatCompletionsUpdate
        | ChatCompletion
        | None
    ):
        # is_continue: bool = True
        # max_loop = 50
        max_loop = self._get_max_loop(chat_req)
        logger.bind(ctx=ctx).info(f"Total Chat Loops: {max_loop}")
        curr_loop = 0
        while True:
            app_state.raise_if_cancelled()
            snapshot_openai_chat(ctx, app_state, chat_req, curr_loop)
            if curr_loop >= max_loop:
                logger.bind(ctx=ctx).error(f"Chat loop exceeded max loop: {max_loop}")
                raise RecursionError({"detail": f"Chat loop exceeded max loop: {max_loop}"})
            curr_loop += 1
            logger.bind(ctx=ctx).info(f"Chat Loop: {curr_loop}")

            chat_completion_config = await OpenaiChat.get_model_based_config(
                ctx=ctx, chat_req=chat_req
            )

            client: AsyncAzureOpenAI | ChatCompletionsClient | AsyncOpenAI | None = None
            if self.provider == "azure":
                logger.bind(ctx=ctx).info(f"Using Azure OpenAI client with model: {chat_req.model}")
                client: (
                    AsyncAzureOpenAI | ChatCompletionsClient
                ) = await self.client_factory.create_client(model=chat_req.model)
            elif self.provider in ("openai", "azure_byok"):
                logger.bind(ctx=ctx).info(f"Using {self.provider} chat client")
                client: AsyncOpenAI = self.client
            assert client, "Openai LLM chat Client not found"

            chat_completion: (
                ChatCompletion
                | AsyncStream[ChatCompletionChunk]
                | StreamingChatCompletionsUpdate
                | None
            ) = None
            match client:
                case AsyncOpenAI():
                    logger.bind(ctx=ctx, organization=client.organization).info(
                        "Using Async OpenAI client"
                    )
                    notify(ctx, "OpenAI thinking ...") if notify else None
                    chat_completion: (
                        ChatCompletion | AsyncStream[ChatCompletionChunk]
                    ) = await client.chat.completions.create(**chat_completion_config)
                    if isinstance(chat_completion, AsyncStream):
                        return chat_completion
                case AsyncAzureOpenAI():
                    logger.bind(ctx=ctx, organization=client.organization).info(
                        "Using Async Azure OpenAI client"
                    )
                    chat_completion: (
                        ChatCompletion | AsyncStream[ChatCompletionChunk]
                    ) = await client.chat.completions.create(**chat_completion_config)
                    if isinstance(chat_completion, AsyncStream):
                        return chat_completion
                case ChatCompletionsClient():
                    chat_azure_inference: (
                        ChatCompletions | StreamingChatCompletionsUpdate
                    ) = await client.complete(**chat_completion_config)
                    match chat_azure_inference:
                        case ChatCompletions():
                            chat_completion = ChatCompletion(**chat_azure_inference)
                        case StreamingChatCompletionsUpdate():
                            return chat_azure_inference
                case _:
                    raise AssertionError("Openai LLM chat Client not found")

            TokenUsageService.record(
                ctx=ctx,
                surface=LlmSurface.openai_chat,
                app_state=app_state,
                usage=getattr(chat_completion, "usage", None),
                model=str(getattr(chat_completion, "model", None) or chat_req.model),
                loop=curr_loop,
            )

            choice = chat_completion.choices[-1]
            logger.bind(
                ctx=ctx
            ).info("Openai Chat Choice")
            chat_req.messages.append(choice.message.model_dump(exclude_none=True))

            if choice.message.tool_calls or choice.finish_reason == "tool_calls":
                messages = await self.run_tools(
                    ctx=ctx,
                    app_state=app_state,
                    choice=choice,
                    user=user,
                    db=db,
                    notify=notify,
                    tool_output_text_to_file_char_limit=tool_output_text_to_file_char_limit,

                )
                chat_req.messages += messages

            if choice.finish_reason == "content_filter":
                if choice.message.content:
                    return chat_completion
                else:
                    choice.message.content = "Can't generate due to content filter policies"
            if choice.finish_reason == "tool_calls":
                continue
            elif choice.finish_reason == "stop":
                return chat_completion
            elif choice.finish_reason == "length":
                message: ChatCompletionSystemMessageParam = {
                    "role": "system",
                    "content": "Continue",
                }
                chat_req.messages += [message]
                continue
                # return chat_completion  # TODO: length to stop
            else:
                return chat_completion

    async def chat_loop_stream(
        self,
        ctx: Context,
        app_state: AppState,
        chat_req: ChatReq,
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase | None = None,
        notify: Notify | None = None,
        tool_output_text_to_file_char_limit: int | None = None,

    ) -> (
        AsyncStream[ChatCompletionChunk]
        | ChatCompletions
        | StreamingChatCompletionsUpdate
        | ChatCompletion
        | None
    ):
        # is_continue: bool = True
        # max_loop = 20
        max_loop = self._get_max_loop(chat_req)
        curr_loop = 0
        content_main = ""
        content_complimentary = ""

        notify_main = (
            Notify(topic=text_topic(notify.topic), payload=NotifyPayload.snapshot)
            if notify
            else None
        )
        notify_complimentary = Notify(topic=notify.topic) if notify else None
        while True:
            app_state.raise_if_cancelled()
            snapshot_openai_chat(ctx, app_state, chat_req, curr_loop)
            if curr_loop >= max_loop:
                logger.bind(ctx=ctx).error(f"Chat loop exceeded max loop: {max_loop}")
                raise RecursionError({"detail": f"Chat loop exceeded max loop: {max_loop}"})
            curr_loop += 1
            logger.bind(ctx=ctx).info(f"Chat Loop: {curr_loop}")

            chat_completion_config = await OpenaiChat.get_model_based_config(
                ctx=ctx, chat_req=chat_req
            )
            chat_completion_config["stream_options"] = {"include_usage": True}

            client: AsyncAzureOpenAI | ChatCompletionsClient | AsyncOpenAI | None = None
            if self.provider == "azure":
                logger.bind(ctx=ctx).info(f"Using Azure OpenAI client with model: {chat_req.model}")
                client: (
                    AsyncAzureOpenAI | ChatCompletionsClient
                ) = await self.client_factory.create_client(model=chat_req.model)
            elif self.provider in ("openai", "azure_byok"):
                logger.bind(ctx=ctx).info(f"Using {self.provider} chat client")
                client: AsyncOpenAI = self.client
            assert client, "Openai LLM chat Client not found"

            chat_completion_stream: AsyncStream[ChatCompletionChunk] = None
            match client:
                case AsyncOpenAI():
                    logger.bind(ctx=ctx, organization=client.organization).info(
                        "Using Async OpenAI client"
                    )
                    chat_completion_stream: AsyncStream[
                        ChatCompletionChunk
                    ] = await client.chat.completions.create(**chat_completion_config)
                case AsyncAzureOpenAI():
                    logger.bind(ctx=ctx, organization=client.organization).info(
                        "Using Async Azure OpenAI client"
                    )
                    chat_completion_stream: AsyncStream[
                        ChatCompletionChunk
                    ] = await client.chat.completions.create(**chat_completion_config)
                # case ChatCompletionsClient():
                #     chat_azure_inference: (
                #         ChatCompletions | StreamingChatCompletionsUpdate
                #     ) = await client.complete(**chat_completion_config)
                #     match chat_azure_inference:
                #         case ChatCompletions():
                #             chat_completion = ChatCompletion(**chat_azure_inference)
                #         case StreamingChatCompletionsUpdate():
                #             return chat_azure_inference
                case _:
                    raise AssertionError("Openai LLM chat Client not found")

            chat_completion_message: None

            tool_calls_req = []
            finish_reason = None
            id_completion = None
            created = None
            model = None
            system_fingerprint = None
            usage = None
            async for chunk in chat_completion_stream:
                # choice = chunk.choices[0]
                if not id_completion and chunk.id:
                    id_completion = chunk.id
                if not created and chunk.created:
                    created = chunk.created
                if not model and chunk.model:
                    model = chunk.model
                if not system_fingerprint and chunk.system_fingerprint:
                    system_fingerprint = chunk.system_fingerprint
                if not usage and chunk.usage:
                    usage = chunk.usage
                for choice in chunk.choices:
                    if choice.delta.role:
                        logger.bind(ctx=ctx, role=choice.delta.role).info(
                            f"Role in delta : {choice.delta.role}"
                        )
                        # yield send("role", role=choice.delta.role)
                        chat_completion_message = {
                            "role": choice.delta.role,
                            "content": choice.delta.content or "",
                        }
                    if choice.delta.refusal:
                        logger.bind(ctx=ctx, refusal=choice.delta.refusal).info(
                            f"Refusal in delta : {choice.delta.refusal}"
                        )
                        # yield send("refusal", refusal=choice.delta.refusal)

                        if chat_completion_message:
                            if chat_completion_message.get("refusal"):
                                content_complimentary += choice.delta.refusal
                                chat_completion_message["refusal"] += choice.delta.refusal
                            else:
                                content_complimentary = choice.delta.refusal
                                chat_completion_message["refusal"] = choice.delta.refusal
                        else:
                            chat_completion_message = {
                                "role": choice.delta.role or "assistant",
                                "content": choice.delta.content or "",
                                "refusal": choice.delta.refusal,
                            }
                            content_complimentary = choice.delta.refusal
                        notify_complimentary(ctx, content_complimentary)
                    if choice.delta.content:
                        logger.bind(ctx=ctx, content=choice.delta.content).info(
                            f"Content in delta : {choice.delta.content}"
                        )
                        # yield send("content-bytes", content=choice.delta.content)
                        content_main += choice.delta.content
                        notify_main(ctx, content_main)
                        if chat_completion_message:
                            if chat_completion_message.get("content"):
                                chat_completion_message["content"] += choice.delta.content
                            else:
                                chat_completion_message["content"] = choice.delta.content
                        else:
                            chat_completion_message = {
                                "role": choice.delta.role or "assistant",
                                "content": choice.delta.content,
                            }

                    if choice.delta.tool_calls:
                        for tool_call in choice.delta.tool_calls:
                            if tool_call.id:
                                logger.bind(
                                    ctx=ctx,
                                    tool_call_id=tool_call.id,
                                    tool_name=tool_call.function.name,
                                    tool_arguments=tool_call.function.arguments,
                                ).info(f"Tool call in delta : {tool_call.id}")
                                # yield send("tool_call_start", tool_call_id=tool_call.id, tool_name=tool_call.function.name, tool_arguments=tool_call.function.arguments)
                                content_complimentary = f"Calling tool {tool_call.function.name} with arguments {tool_call.function.arguments}"
                                notify_complimentary(ctx, content_complimentary)
                                tool_calls_req.append(
                                    {
                                        "id": tool_call.id,
                                        "name": tool_call.function.name or "",
                                        "arguments": tool_call.function.arguments or "",
                                    }
                                )
                            else:
                                tool_calls_req[-1]["arguments"] += tool_call.function.arguments
                                content_complimentary += tool_call.function.arguments
                                notify_complimentary(ctx, content_complimentary)
                    if choice.finish_reason:
                        logger.bind(ctx=ctx, finish_reason=choice.finish_reason).info(
                            f"Finish reason in delta : {choice.finish_reason}"
                        )
                        # yield send("finish", finish_reason=choice.finish_reason)
                        content_complimentary = f"Finish reason: {choice.finish_reason}"
                        notify_complimentary(ctx, content_complimentary)
                        finish_reason = choice.finish_reason

            choice_payload = {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {
                    "role": chat_completion_message["role"] or "assistant",
                    # If you prefer empty string instead of None, drop the `or None`
                    "content": chat_completion_message["content"] or None,
                },
            }
            for tc in tool_calls_req:
                obj = {
                    "id": tc["id"],
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                    },
                    "type": "function",
                }
                choice_payload["message"].setdefault("tool_calls", []).append(obj)

            chat_completion_obj: ChatCompletion = ChatCompletion.model_validate(
                {
                    "id": id_completion,
                    "object": "chat.completion",
                    "created": created,
                    "model": model,
                    "usage": usage,
                    "choices": [choice_payload],
                }
            )
            TokenUsageService.record(
                ctx=ctx,
                surface=LlmSurface.openai_chat,
                app_state=app_state,
                usage=usage,
                model=str(model or chat_req.model),
                loop=curr_loop,
            )

            # choice = chat_completion.choices[-1]
            logger.bind(ctx=ctx, chat_config=chat_completion_config).info("Openai Chat Choice")

            if len(tool_calls_req) > 0 or finish_reason == "tool_calls":
                assistant_tool_msg = {
                    "role": "assistant",
                    # when tool_calls are present, content is commonly None (allowed)
                    "content": None
                    if chat_completion_message is None
                    else chat_completion_message.get("content"),
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"] or "",
                                "arguments": tc["arguments"] or "",
                            },
                        }
                        for tc in tool_calls_req
                    ],
                }
                # This MUST be immediately before any role=tool messages
                chat_req.messages.append(assistant_tool_msg)
                messages = await self.run_tools_stream(
                    ctx=ctx,
                    app_state=app_state,
                    tool_calls=tool_calls_req,
                    user=user,
                    db=db,
                    notify=notify,
                    tool_output_text_to_file_char_limit=tool_output_text_to_file_char_limit,

                )
                chat_req.messages += messages
            else:
                chat_req.messages.append(chat_completion_message)

            if finish_reason == "content_filter":
                # if not chat_completion_message or not chat_completion_message.content:
                #     choice.message.content = "Can't generate due to content filter policies"
                # yield send("chat-loop-stream",result=chat_completion_obj.model_dump(exclude_none=True))
                return chat_completion_obj  # TODO: content_filter to stop

            if choice.finish_reason == "tool_calls":
                continue
            elif choice.finish_reason == "stop":
                # yield send("chat-loop-stream",result=chat_completion_obj.model_dump(exclude_none=True))
                return chat_completion_obj  # finished

            elif choice.finish_reason == "length":
                message: ChatCompletionSystemMessageParam = {
                    "role": "system",
                    "content": "Continue",
                }
                chat_req.messages += [message]
                continue
                # return chat_completion  # TODO: length to stop
            else:
                # yield send("chat-loop-stream",result=chat_completion_obj.model_dump(exclude_none=True))
                # break
                return chat_completion_obj  # finished

    async def yield_chat_chunks(
        self, chat_res: AsyncStream[ChatCompletionChunk]
    ) -> ChatCompletionChunk | None:
        try:
            async for part in chat_res:
                yield part
        except Exception as e:
            logger.error(str(e))

    async def run_tools_stream(
        self,
        ctx: Context,
        app_state: AppState,
        tool_calls,
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase | None = None,
        notify: Notify | None = None,
        tool_output_text_to_file_char_limit: int | None = None,

    ) -> List[ChatCompletionMessageParam | ChatCompletionMessage]:
        messages = []
        for tool_call in tool_calls:
            tool_call_id = tool_call["id"]
            tool_name = tool_call["name"]
            tool_args = tool_call["arguments"]
            try:
                # run tool
                tool_output: DataBlock = await ToolFactory(user=user, db=db).run_tool(
                    ctx,
                    app_state,
                    tool_name,
                    tool_args,
                    notify=notify,
                    output_text_to_file_char_limit=tool_output_text_to_file_char_limit,
                )
                tool_output_str = tool_output.get_llm_prompt(ctx=ctx)
                logger.bind(
                    ctx=ctx,
                    app_code=AppCode.ACTION,
                    tool=tool_name,
                    tool_input=tool_args,
                    tool_output=tool_output_str,
                ).info(f"Ran tool: {tool_name}")
                # add tool call output to Messages
                tool_output_message = ChatCompletionToolMessageParam(
                    role="tool", content=tool_output_str, tool_call_id=tool_call_id
                )
                messages.append(tool_output_message)
                # await self.chat_loop(chat_req)
            except Exception as e:
                logger.bind(
                    ctx=ctx, app_code=AppCode.ACTION, tool=tool_name, tool_error=str(e)
                ).exception(f"Error in tool_call: {str(e)}")
                # add tool call error to Messages
                tool_error_message = ChatCompletionToolMessageParam(
                    role="tool", content=str(e), tool_call_id=tool_call_id
                )

                messages.append(tool_error_message)
        return messages

    async def run_tools(
        self,
        ctx: Context,
        app_state: AppState,
        choice,
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase | None = None,
        notify: Notify | None = None,
        tool_output_text_to_file_char_limit: int | None = None,

    ) -> List[ChatCompletionMessageParam | ChatCompletionMessage]:
        messages = []
        for tool_call in choice.message.tool_calls:
            tool_call_id = tool_call.id
            tool_name = tool_call.function.name
            tool_args = tool_call.function.arguments
            try:
                # run tool
                tool_output: DataBlock = await ToolFactory(user=user, db=db).run_tool(
                    ctx,
                    app_state,
                    tool_name,
                    tool_args,
                    notify=notify,
                    output_text_to_file_char_limit=tool_output_text_to_file_char_limit,
                )
                tool_output_str = tool_output.get_llm_prompt(ctx=ctx)
                logger.bind(
                    ctx=ctx,
                    app_code=AppCode.ACTION,
                    tool=tool_name,
                    tool_input=tool_args,
                    tool_output=tool_output,
                ).info(f"Ran tool: {tool_name}")
                # add tool call output to Messages
                tool_output_message = ChatCompletionToolMessageParam(
                    role="tool", content=tool_output_str, tool_call_id=tool_call_id
                )
                messages.append(tool_output_message)
                # await self.chat_loop(chat_req)
            except Exception as e:
                logger.bind(
                    ctx=ctx, app_code=AppCode.ACTION, tool=tool_name, tool_error=str(e)
                ).exception(f"Error in tool_call: {str(e)}")
                # add tool call error to Messages
                tool_error_message = ChatCompletionToolMessageParam(
                    role="tool", content=str(e), tool_call_id=tool_call_id
                )

                messages.append(tool_error_message)
        return messages

    @staticmethod
    async def get_model_based_config(ctx: Context, chat_req: ChatReq) -> Dict[str, Any]:
        chat_req_dict = chat_req.model_dump(exclude_none=True)

        # Handle structured outputs
        structured_output = chat_req.get_structured_output_format()
        if structured_output:
            # Remove any existing response_format if we're going to replace it
            if "response_format" in chat_req_dict:
                chat_req_dict.pop("response_format")
            chat_req_dict["response_format"] = structured_output

        # Remove json_schema and response_schema fields as they're not part of OpenAI's API
        if "json_schema" in chat_req_dict:
            chat_req_dict.pop("json_schema")
        if "response_schema" in chat_req_dict:
            chat_req_dict.pop("response_schema")

        # ["gpt-5.1-chat-latest", "gpt-5.2-chat-latest", "gpt-5.3-chat-latest", "gpt-5-chat-latest"]
        if chat_req.model.startswith("o") or (
            (chat_req.model.startswith("gpt-5") or chat_req.model.startswith("gpt-6"))
            and (
                chat_req.model
                not in ["gpt-5-chat-latest"]
            )
        ):
            logger.bind(ctx=ctx).info("Applying o-series/gpt-5 specific config adjustments")
            if "max_tokens" in chat_req_dict:
                max_tokens = chat_req_dict.pop("max_tokens")
                chat_req_dict["max_completion_tokens"] = max_tokens
            if "temperature" in chat_req_dict:
                chat_req_dict.pop("temperature")
            if "stop" in chat_req_dict:
                chat_req_dict.pop("stop")
            if "top_p" in chat_req_dict:
                chat_req_dict.pop("top_p")

        if chat_req.model.startswith("gpt-5.2-pro"):
            logger.bind(ctx=ctx).info("Applying gpt-5.2-pro specific config adjustments")
            if "verbosity" in chat_req_dict:
                chat_req_dict.pop("verbosity")
            if "reasoning_effort" in chat_req_dict:
                chat_req_dict.pop("reasoning_effort")

        # Non-reasoning models: gpt-3.5* and gpt-4* do not support reasoning_effort
        if chat_req.model.startswith("gpt-3.5") or chat_req.model.startswith("gpt-4"):
            logger.bind(ctx=ctx, model=chat_req.model).info(
                "Removing reasoning_effort for non-reasoning model"
            )
            chat_req_dict.pop("reasoning_effort", None)

        # # Handle gpt-5 variants
        # if chat_req.model.startswith("gpt-5"):
        #     if "temperature" in chat_req_dict:
        #         chat_req_dict.pop("temperature")

        # Model-specific configurations
        match chat_req.model:
            case "gpt-4o-1120":
                chat_req.model = "gpt-4o"
            case "gpt-4o-0806":
                chat_req.model = "gpt-4o"
            case "gpt-4o-0513":
                chat_req.model = "gpt-4o"
            # gpt-5 case removed - now handled by startswith check above
            case "deepseek-r1":
                chat_req_dict.pop("n") if "n" in chat_req_dict else None
                chat_req_dict.pop("user") if "user" in chat_req_dict else None

        # Log structured output format if present
        if structured_output:
            logger.bind(ctx=ctx, response_format=structured_output).info(
                "Using structured output format"
            )

        return chat_req_dict
