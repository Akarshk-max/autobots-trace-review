import io
from typing import Any, Dict, List, Literal, Type

from azure.ai.inference.aio import ChatCompletionsClient
from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai import AsyncAzureOpenAI, AsyncOpenAI, AsyncStream, BadRequestError, _legacy_response
from openai.types.responses import (  # ToolParam,; --- add all stream event subtypes ---
    Response,
    ResponseCodeInterpreterToolCall,
    ResponseCodeInterpreterToolCallParam,
    ResponseComputerToolCall,
    ResponseComputerToolCallParam,
    ResponseFileSearchToolCall,
    ResponseFileSearchToolCallParam,
    ResponseFunctionToolCall,
    ResponseFunctionToolCallParam,
    ResponseFunctionWebSearch,
    ResponseFunctionWebSearchParam,
    ResponseInputItemParam,
    ResponseInputTextParam,
    ResponseOutputItem,
    ResponseOutputMessage,
    ResponseOutputMessageParam,
    ResponseReasoningItem,
    ResponseReasoningItemParam,
    ResponseStreamEvent,
    response_output_item,
)
from openai.types.responses.response_input_item_param import (
    FunctionCallOutput,
    ImageGenerationCall,
    McpApprovalRequest,
    McpCall,
    McpListTools,
)
from openai.types.responses.response_input_param import LocalShellCall, Message
from tenacity import retry, stop_after_attempt, wait_fixed

from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.openai.azure_openai_client_factory import AzureOpenAIModelClientFactory
from src.autobots.conn.openai.openai_responses.openai_responses import LlmResponsesOpenaiConfig
from src.autobots.conn.openai.openai_responses.response_model import OpenaiResponsesReq
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.llm.tools.tool_factory import ToolFactory
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.server_sent_events.topics import text_topic
from src.autobots.user.user_orm_model import UserORM


class OpenaiResponsesStream:
    def __init__(
        self,
        provider: Literal["azure", "openai"],
        client_factory: Type[AzureOpenAIModelClientFactory] | None = None,  # for azure
        openai_client: AsyncOpenAI | None = None,  # for openai
    ):
        assert client_factory or openai_client, (
            "Either client_factory or openai_client must be provided"
        )
        self.provider = provider
        self.client = openai_client
        self.client_factory = client_factory

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(3), reraise=True)
    async def create(
        self,
        responses_req: LlmResponsesOpenaiConfig,
        app_state: AppState,
        user: UserORM | None = None,
        ctx: Context | None = None,
        db: AsyncIOMotorDatabase | None = None,
        notify: Notify | None = None,
    ) -> Response:
        try:
            logger.bind(ctx=ctx).trace("Starting OpenAI Responses, try: 1")
            client: AsyncAzureOpenAI | ChatCompletionsClient | AsyncOpenAI | None = None
            if self.provider == "azure":
                logger.bind(ctx=ctx).info(
                    f"Using Azure OpenAI client with model: {responses_req.model}"
                )
                client: (
                    AsyncAzureOpenAI | ChatCompletionsClient
                ) = await self.client_factory.create_client(model=responses_req.model)
            elif self.provider == "openai":
                logger.bind(ctx=ctx).info("Using OpenAI responses client")
                client: AsyncOpenAI = self.client
            assert client, "Openai LLM responses Client not found"

            user_files: UserFiles | None = None
            if user:
                s3 = get_public_s3()
                user_files = UserFiles(user=user, s3=s3)

            final_response: Response = await self.response_loop_stream(
                ctx=ctx,
                app_state=app_state,
                client=client,
                responses_req=responses_req,
                user=user,
                db=db,
                user_files=user_files,
                notify=notify,
            )
            # final_response: Response | None = None
            # async for chunk in agen:
            #     try:
            #         obj = json.loads(chunk.decode("utf-8"))
            #         if obj.get("event") == "response-loop":
            #             final_response = obj.get("error")
            #             if final_response is None:
            #                 if (obj.get("result") is None):
            #                     yield send("error", message=f"response-loop error - returned None in return value : {final_response}")
            #                 final_response = Response.model_validate(obj.get("result"))
            #             else:
            #                 raise Exception(f"Error in response-loop : {final_response}")
            #         yield chunk                     # forward chunk to caller
            #     except Exception:
            #         raise
            # yield send("create",result=final_response.model_dump(exclude_none=True))
            return final_response
        except Exception as e:
            logger.bind(
                ctx=ctx, responses_req=responses_req.model_dump(exclude_none=True)
            ).exception(f"Error in Openai Responses: {str(e)}")
            raise

    async def response_loop_stream(
        self,
        ctx: Context,
        app_state: AppState,
        client: AsyncAzureOpenAI | ChatCompletionsClient | AsyncOpenAI,
        responses_req: LlmResponsesOpenaiConfig,
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase | None = None,
        user_files: UserFiles | None = None,
        notify: Notify | None = None,
    ) -> Response:
        max_loop = 20
        curr_loop = 0
        final_response = None
        # def send(ev: str, **kw) -> bytes:
        #     logger.bind(
        #         ctx=ctx, responses_req=responses_req.model_dump(exclude_none=True)
        #     ).info(f"Sending event: {ev} with data: {kw}")
        #     return (json.dumps({"event": ev, **kw}, ensure_ascii=False) + "\n").encode("utf-8")

        content_main = ""
        content_complimentary = ""

        notify_main = Notify(topic=text_topic(notify.topic)) if notify else None
        notify_complimentary = Notify(topic=notify.topic) if notify else None
        while True:
            if curr_loop >= max_loop:
                logger.bind(ctx=ctx).error(f"Responses loop exceeded max loop: {max_loop}")
                raise RecursionError({"detail": f"Responses loop exceeded max loop: {max_loop}"})
            curr_loop += 1
            logger.bind(ctx=ctx).info(f"Responses Loop: {curr_loop}")

            # responses_config = await OpenaiResponses.get_model_based_config(ctx=ctx, responses_req=responses_req)

            response: Response | None = None
            # response_error: Exception | None = None
            if not responses_req.stream:
                logger.bind(ctx=ctx).info("Setting stream=True for responses_req")
                responses_req.stream = True
            openai_responses_req = OpenaiResponsesReq(**responses_req.model_dump(exclude_none=True))
            try:
                # print("responses_req: ", responses_req)
                logger.bind(
                    ctx=ctx, responses_req=responses_req.model_dump(exclude_none=True)
                ).info(
                    f"Creating OpenAI responses stream object {openai_responses_req.model_dump(exclude_none=True)}"
                )
                stream_obj: AsyncStream[ResponseStreamEvent] = await client.responses.create(
                    **openai_responses_req.model_dump(exclude_none=True)
                )
                # print("stream_obj: ", stream_obj)
                logger.bind(
                    ctx=ctx, responses_req=responses_req.model_dump(exclude_none=True)
                ).info(f"Created OpenAI responses stream object {stream_obj}")
                loop_needed: bool = False
                notify_complimentary(ctx, "starting response loop")
                try:
                    # iterate the stream as you already do
                    async for event in stream_obj:  # type: ignore
                        # print("event: ", event)
                        # logger.bind(
                        #     ctx=ctx, responses_req=responses_req.model_dump(exclude_none=True)
                        # ).info(f"Received event: {event}")
                        match event.type:
                            # text & refusal deltas
                            case "response.created":
                                # yield send("token_2", message="Starting to create your response...")
                                content_complimentary = "Starting to create your response..."
                                notify_complimentary(ctx, content_complimentary)
                            case "response.in_progress":
                                # yield send("token_2", message="Response in progress...")
                                content_complimentary = "Response in progress..."
                                notify_complimentary(ctx, content_complimentary)
                            case "response.output_text.delta":
                                # yield send("token", text=event.delta)
                                content_main += event.delta
                                notify_main(ctx, content_main)
                            # case "response.output_text.done":
                            #     # yield send("token", text=event.text)
                            #     content_main += event.text
                            #     notify_main(ctx,content_main)
                            # case "respons"

                            # case "response.refusal.delta":
                            #     # yield send("token_2", text=event.delta)

                            #     notify_complimentary(ctx,event.delta)
                            # code interpreter lifecycle
                            case "response.code_interpreter_call.in_progress":
                                # yield send("token_2", text="code interpreter in progress...")
                                content_complimentary = "code interpreter in progress..."
                                notify_complimentary(ctx, content_complimentary)
                            case "response.code_interpreter_call.interpreting":
                                # yield send("token_2", text="code interpreter interpreting...")
                                content_complimentary = "code interpreter interpreting..."
                                notify_complimentary(ctx, content_complimentary)
                            case "response.code_interpreter_call.completed":
                                # yield send("token_2", text="code interpreter completed.")
                                content_complimentary = "code interpreter completed."
                                notify_complimentary(ctx, content_complimentary)

                            # file search lifecycle
                            case "response.file_search_call.in_progress":
                                # yield send("token_2", text="file search in progress")
                                content_complimentary = "file search in progress"
                                notify_complimentary(ctx, content_complimentary)
                            case "response.file_search_call.searching":
                                # yield send("token_2", text="file search searching")
                                content_complimentary = "file search searching"
                                notify_complimentary(ctx, content_complimentary)
                            case "response.file_search_call.completed":
                                # yield send("token_2", text="file search completed")
                                content_complimentary = "file search completed"
                                notify_complimentary(ctx, content_complimentary)

                            case "response.web_search_call.in_progress":
                                # yield send("token_2", text="web search in progress")
                                content_complimentary = "web search in progress"
                                notify_complimentary(ctx, content_complimentary)
                            case "response.web_search_call.searching":
                                # yield send("token_2", text="web search searching")
                                content_complimentary = "web search searching"
                                notify_complimentary(ctx, content_complimentary)
                            case "response.web_search_call.completed":
                                # yield send("token_2", text="web search completed")
                                content_complimentary = "web search completed"
                                notify_complimentary(ctx, content_complimentary)

                            # reasoning tokens (o-series)
                            case "response.reasoning_summary_part.added":
                                # yield send("reasoning-summary", text="Reasoning summary being added...")
                                content_complimentary = "Reasoning summary being added..."
                                notify_complimentary(ctx, content_complimentary)
                            case "response.reasoning_summary_part.done":
                                # yield send("reasoning-summary", text=event.part.text)
                                content_complimentary += event.part.text
                                notify_complimentary(ctx, content_complimentary)
                            case "response.reasoning_summary_text.delta":
                                # yield send("reasoning-summary", text=event.delta)
                                content_complimentary += event.delta
                                notify_complimentary(ctx, content_complimentary)
                            # case "response.reasoning_text.delta":
                            #     yield send("reasoning", text=f"{event.delta}")

                            # image generation lifecycle (names per docs; adjust if your tool emits slightly different ones)
                            case "response.image_generation_call.in_progress":
                                # yield send("token_2", text="image generation in progress...")
                                content_complimentary = "image generation in progress..."
                                notify_complimentary(ctx, content_complimentary)
                            case "response.image_generation_call.completed":
                                # yield send("token_2", text="image generation completed")
                                content_complimentary = "image generation completed"
                                notify_complimentary(ctx, content_complimentary)
                            case "response.image_generation_call.generating":
                                # yield send("token_2", text="image generation generating")
                                content_complimentary = "image generation generating"
                                notify_complimentary(ctx, content_complimentary)
                            case "response.image_generation_call.partial_image":
                                # yield send("partial-image", text=event.partial_image_b64)
                                notify_complimentary(ctx, event.partial_image_b64)
                            # output item add: function tool call starting
                            case "response.output_item.added":
                                # your body expects `fc.name` already bound
                                if event.item.type == "function_call":
                                    # yield send("function_call", text=f"Function call started: {event.item.name}")
                                    content_complimentary = (
                                        f"Function call started: {event.item.name}"
                                    )
                                    notify_complimentary(ctx, content_complimentary)
                            # function call arguments done
                            case "response.function_call_arguments.done":
                                # yield send("function_call", text=f"Sending arguments : {event.arguments}")
                                content_complimentary = f"Sending arguments : {event.arguments}"
                                notify_complimentary(ctx, content_complimentary)

                            # completed: final response object
                            case "response.completed":
                                # print("response completed")
                                response = event.response
                                is_next_loop_needed = False
                                final_response = response
                                logger.bind(
                                    ctx=ctx,
                                    event=event,
                                    response=response.model_dump(exclude_none=True),
                                ).info(f"Response completed {final_response}")
                                response_outputs: List[ResponseOutputItem] = response.output
                                for response_output in response_outputs:
                                    logger.bind(
                                        ctx=ctx, response_output=response_output.model_dump()
                                    ).info("Openai Response Output")

                                    if await self.is_next_loop_needed(response_output):
                                        is_next_loop_needed = True

                                    responses_output_to_input = (
                                        await self.convert_output_item_to_input_item_param(
                                            ctx=ctx,
                                            responses_req=responses_req,
                                            output_item=response_output,
                                            store=responses_req.store,
                                            client=client,
                                            user_files=user_files,
                                        )
                                    )
                                    if responses_output_to_input:
                                        responses_req.input.append(responses_output_to_input)

                                    response_input_item_param = (
                                        await self.process_response_output_item(
                                            ctx=ctx,
                                            app_state=app_state,
                                            response_output_item=response_output,
                                            user=user,
                                            db=db,
                                        )
                                    )
                                    if response_input_item_param:
                                        responses_req.input.append(response_input_item_param)

                                    loop_needed = is_next_loop_needed

                            # failed
                            case "response.failed":
                                logger.bind(ctx=ctx).error("Response failed")
                                response = event.response
                                err = getattr(response, "error", None)
                                inc = getattr(response, "incomplete_details", None)
                                details = {
                                    "response_id": response.id,
                                    "status": response.status,
                                    "model": getattr(response, "model", None),
                                    "error": {
                                        "type": getattr(err, "type", None),
                                        "code": getattr(err, "code", None),
                                        "message": getattr(err, "message", None)
                                        or (str(err) if err else None),
                                        "param": getattr(err, "param", None),
                                    }
                                    if err
                                    else None,
                                    "incomplete_details": {
                                        "reason": getattr(inc, "reason", None),
                                        "duration": getattr(inc, "duration", None),
                                        "token_limit": getattr(inc, "token_limit", None),
                                    }
                                    if inc
                                    else None,
                                }
                                raise HTTPException(
                                    status_code=502, detail=details
                                )  # Bad Gateway to indicate upstream failure
                            case "response.incomplete":
                                logger.bind(ctx=ctx).error("Response incomplete")
                                response = event.response
                                err = getattr(response, "error", None)
                                inc = getattr(response, "incomplete_details", None)
                                details = {
                                    "response_id": response.id,
                                    "status": response.status,
                                    "model": getattr(response, "model", None),
                                    "error": {
                                        "type": getattr(err, "type", None),
                                        "code": getattr(err, "code", None),
                                        "message": getattr(err, "message", None)
                                        or (str(err) if err else None),
                                        "param": getattr(err, "param", None),
                                    }
                                    if err
                                    else None,
                                    "incomplete_details": {
                                        "reason": getattr(inc, "reason", None),
                                        "duration": getattr(inc, "duration", None),
                                        "token_limit": getattr(inc, "token_limit", None),
                                    }
                                    if inc
                                    else None,
                                }
                                raise HTTPException(
                                    status_code=502, detail=details
                                )  # Bad Gateway to indicate upstream failure
                            case _:
                                logger.bind(ctx=ctx).warning(f"Unhandled event type: {event.type}")
                                continue
                    if loop_needed:
                        # yield send("info", message="Loop needed, continuing to next loop")

                        continue
                    else:
                        # yield send("done",message="No loop needed, finishing")
                        # yield send("response-loop", result=final_response.model_dump(exclude_none=True))
                        notify_complimentary(ctx, "Finishing")
                        logger.bind(ctx=ctx).info("Responses loop finished, no next loop needed")
                        # break
                        # logger.bind(ctx=ctx).info(f"Final response: {final_response}")
                        return final_response

                except Exception:
                    # yield send("error", message=f"stream_exception: {e}")
                    raise

                # logger.bind(ctx=ctx).info("Responses loop finished")

            except Exception as e:
                logger.bind(ctx=ctx, error=str(e)).exception(
                    f"Error in response_loop_stream: {str(e)}"
                )
                raise e
                # yield send("response-loop", error=f"response_loop_exception: {str(e)}")

    @staticmethod
    async def get_model_based_config(
        ctx: Context, responses_req: LlmResponsesOpenaiConfig
    ) -> Dict[str, Any]:
        responses_req_dict = responses_req.model_dump(exclude_none=True)
        return responses_req_dict

    @staticmethod
    async def convert_output_item_to_input_item_param(
        ctx: Context,
        responses_req: LlmResponsesOpenaiConfig,
        output_item: ResponseOutputItem,
        store: bool,
        client: AsyncAzureOpenAI | ChatCompletionsClient | AsyncOpenAI,
        user_files: UserFiles | None = None,
    ) -> ResponseInputItemParam | None:
        if store:
            match output_item:
                case ResponseOutputMessage():
                    await OpenaiResponsesStream.update_openai_urls_with_public_s3_urls(
                        ctx=ctx,
                        responses_req=responses_req,
                        output_item=output_item,
                        client=client,
                        user_files=user_files,
                    )
                    return ResponseOutputMessageParam(**output_item.model_dump(exclude_none=True))
                case ResponseFileSearchToolCall():
                    return ResponseFileSearchToolCallParam(
                        **output_item.model_dump(exclude_none=True)
                    )
                case ResponseFunctionToolCall():
                    return ResponseFunctionToolCallParam(
                        **output_item.model_dump(exclude_none=True)
                    )
                case ResponseFunctionWebSearch():
                    return ResponseFunctionWebSearchParam(
                        **output_item.model_dump(exclude_none=True)
                    )
                case ResponseComputerToolCall():
                    return ResponseComputerToolCallParam(
                        **output_item.model_dump(exclude_none=True)
                    )
                case ResponseReasoningItem():
                    return ResponseReasoningItemParam(**output_item.model_dump(exclude_none=True))
                case response_output_item.ImageGenerationCall():
                    return ImageGenerationCall(**output_item.model_dump(exclude_none=True))
                case ResponseCodeInterpreterToolCall():
                    return ResponseCodeInterpreterToolCallParam(**output_item.model_dump())
                case response_output_item.LocalShellCall():
                    return LocalShellCall(**output_item.model_dump(exclude_none=True))
                case response_output_item.McpCall():
                    return McpCall(**output_item.model_dump(exclude_none=True))
                case response_output_item.McpListTools():
                    return McpListTools(**output_item.model_dump(exclude_none=True))
                case response_output_item.McpApprovalRequest():
                    return McpApprovalRequest(**output_item.model_dump(exclude_none=True))
                case _:
                    logger.error(f"Unhandled output item type: {type(output_item)}")
                    raise HTTPException(
                        status_code=501, detail=f"Unhandled output item type: {type(output_item)}"
                    )
        else:
            match output_item:
                case ResponseOutputMessage():
                    await OpenaiResponsesStream.update_openai_urls_with_public_s3_urls(
                        ctx=ctx,
                        responses_req=responses_req,
                        output_item=output_item,
                        client=client,
                        user_files=user_files,
                    )
                    return ResponseOutputMessageParam(**output_item.model_dump(exclude_none=True))
                case ResponseReasoningItem():
                    return ResponseReasoningItemParam(**output_item.model_dump(exclude_none=True))
                case ResponseFunctionToolCall():
                    return ResponseFunctionToolCallParam(
                        **output_item.model_dump(exclude_none=True)
                    )
                case (
                    ResponseFileSearchToolCall()
                    | ResponseFunctionWebSearch()
                    | ResponseComputerToolCall()
                    | ResponseCodeInterpreterToolCall()
                    | ImageGenerationCall()
                    | LocalShellCall()
                ):
                    content = output_item.model_dump_json(exclude_none=True)
                    msg: Message = Message(
                        content=[ResponseInputTextParam(text=content, type="input_text")],
                        role="system",
                    )
                    return msg
                case response_output_item.McpCall():
                    return McpCall(**output_item.model_dump(exclude_none=True))
                case response_output_item.McpListTools():
                    return McpListTools(**output_item.model_dump(exclude_none=True))
                case response_output_item.McpApprovalRequest():
                    return McpApprovalRequest(**output_item.model_dump(exclude_none=True))
                case _:
                    logger.error(f"Unhandled output item type: {type(output_item)}")
                    raise HTTPException(
                        status_code=501, detail=f"Unhandled output item type: {type(output_item)}"
                    )

    async def process_response_output_item(
        self,
        ctx: Context,
        app_state: AppState,
        response_output_item: ResponseOutputItem,
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase | None = None,
    ) -> ResponseInputItemParam | None:
        match response_output_item:
            case ResponseFunctionToolCall():
                function_call_output: FunctionCallOutput = await self.run_tool(
                    ctx=ctx, app_state=app_state, response_output_item=response_output_item, user=user, db=db
                )
                return function_call_output

    async def run_tool(
        self,
        ctx: Context,
        app_state: AppState,
        response_output_item: ResponseFunctionToolCall,
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase | None = None,
    ) -> FunctionCallOutput:
        tool_name = response_output_item.name
        tool_args = response_output_item.arguments
        tool_output_str = ""
        try:
            # run tool
            tool_factory = ToolFactory(user=user, db=db)
            tool_output_str: str = await tool_factory.run_tool(ctx, app_state, tool_name, tool_args)
            logger.bind(
                ctx=ctx,
                app_code=AppCode.ACTION,
                tool=tool_name,
                tool_input=tool_args,
                tool_output=tool_output_str,
            ).info(f"Ran tool: {tool_name}")
        except Exception as e:
            logger.bind(
                ctx=ctx,
                app_code=AppCode.ACTION,
                tool=tool_name,
                tool_input=tool_args,
                tool_error=str(e),
            ).exception(f"Error in tool_call: {str(e)}")
            tool_output_str = str(e)

        tool_call_id = response_output_item.call_id
        function_call_output = FunctionCallOutput(
            type="function_call_output", call_id=tool_call_id, output=tool_output_str
        )

        return function_call_output

    async def is_next_loop_needed(self, response_output_item: ResponseOutputItem) -> bool:
        is_next_loop_needed: bool = False
        match response_output_item:
            case ResponseFunctionToolCall():
                is_next_loop_needed = True
            # case ResponseFileSearchToolCall():
            #     is_next_loop_needed = True
            # case ResponseCodeInterpreterToolCall():
            #     is_next_loop_needed = True
            # case ResponseComputerToolCall():
            #     is_next_loop_needed = True
            # case ResponseFunctionWebSearch():
            #     is_next_loop_needed = True
        return is_next_loop_needed

    @staticmethod
    async def update_openai_urls_with_public_s3_urls(
        ctx: Context,
        responses_req: LlmResponsesOpenaiConfig,
        output_item: ResponseOutputMessage,
        client: AsyncAzureOpenAI | ChatCompletionsClient | AsyncOpenAI,
        user_files: UserFiles | None = None,
    ) -> None:
        if not user_files:
            logger.bind(ctx=ctx, output_item=output_item).error(
                "user_files is None, cant update openai urls with public s3 urls"
            )
            return

        for content in output_item.content:
            for annotation in content.annotations:
                try:
                    res: _legacy_response.HttpxBinaryResponseContent | None = None
                    annotation_type = annotation.type
                    match annotation_type:
                        case "file_citation":
                            file_id = annotation.file_id
                            res = await client.files.content(file_id=file_id)
                        case "url_citation":
                            continue
                        case "container_file_citation":
                            file_id = annotation.file_id
                            container_id = annotation.container_id
                            res = await client.containers.files.content.retrieve(
                                file_id=file_id, container_id=container_id
                            )
                        case "file_path":
                            continue
                    assert res is not None, "res for content is None"
                    assert res.response.status_code == 200, (
                        f"res.response.status_code is not 200: {res.response.status_code}"
                    )
                    file_obj = io.BytesIO(res.content)
                    filename = annotation.filename
                    file_metas = await user_files.upload_binary(
                        ctx=ctx, file_obj=file_obj, filename=filename
                    )
                    assert len(file_metas) > 0
                    responses_req.sandbox_url_to_s3_url[f"sandbox:/mnt/data/{filename}"] = str(
                        file_metas[0].url
                    )
                except Exception as e:
                    if isinstance(e, BadRequestError) and e.message.lower().__contains__(
                        "not allowed to download"
                    ):
                        logger.bind(ctx=ctx, output_item=output_item).info(
                            "Wasn't allowed to download possibly coz it is uploaded by us and not generated by llm"
                        )
                    else:
                        logger.bind(
                            ctx=ctx, output_item=output_item, annotation=annotation, error=str(e)
                        ).exception(f"Error in update_openai_urls_with_public_s3_urls: {str(e)}")

        for sandbox_url, public_s3_url in responses_req.sandbox_url_to_s3_url.items():
            if output_item.content[0].text.__contains__(sandbox_url):
                output_item.content[0].text = output_item.content[0].text.replace(
                    sandbox_url, public_s3_url
                )
            else:
                output_item.content[
                    0
                ].text = f"{output_item.content[0].text}\nfile url: {public_s3_url}"
