from typing import List, Literal, Type

from fastapi import HTTPException
from loguru import logger
from openai.types.chat import (
    ChatCompletionContentPartImageParam,
    ChatCompletionToolParam,
    ChatCompletionUserMessageParam,
)
from openai.types.chat.chat_completion_content_part_image_param import ImageURL

from src.autobots.action.action.action_doc_model import ActionResult
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks, Metadata
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
from src.autobots.conn.openai.openai_chat.chat_model import ChatReq
from src.autobots.core.utils import truncate_str
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.exception.app_exception import AppException
from src.autobots.llm.tools.common_default_tools import OPENAI_CHAT_DEFAULT_TOOLS
from src.autobots.llm.tools.tool_factory import ToolFactory
from src.autobots.llm.tools.tools_map import TOOLS_MAP
from src.autobots.server_sent_events.server_sent_events import Notify


class LlmChatOpenaiConfig(ChatReq):
    provider: Literal["azure", "openai"]
    tool_output_text_to_file_char_limit: int | None = 75000
    tools: List[ChatCompletionToolParam | str] | None = list(OPENAI_CHAT_DEFAULT_TOOLS)


class ActionText2TextLlmChatOpenai(
    ActionABC[LlmChatOpenaiConfig, LlmChatOpenaiConfig, LlmChatOpenaiConfig, DataBlock, DataBlocks]
):
    type = ActionType.text2text_llm_chat_openai

    # https://platform.openai.com/docs/guides/vision/what-type-of-files-can-i-upload#what-type-of-files-can-i-upload
    IMAGE_FORMATS = ["jpeg", "jpg", "png", "gif", "webp"]
    FILE_FORMATS = ["pdf", "docx", "txt", "md", "html", "json", "yaml", "yml"]

    @staticmethod
    def get_description() -> str:
        return (
            "Runs an OpenAI/Azure chat completion with support for multimodal inputs "
            "(images and file URLs), tools, and streaming. The tool builds a chat "
            "request from the provided configuration and any incoming action input, "
            "invokes the configured provider's chat endpoint, and returns the model's "
            "responses as a list of text blocks.\n\n"
            "Input:\n"
            "  {\n"
            '      "provider": "openai" | "azure",            # required\n'
            '      "model": "gpt-4.1" | "gpt-4o-mini" ,       # required\n'
            '      "messages": [ ... ],                           # optional seed messages\n'
            '      "tools": [ "tool_name", { ... tool def ... } ], # optional\n'
            '      "stream": false,                               # optional: enable streaming\n'
            '      "tool_output_text_to_file_char_limit": 75000,  # optional\n'
            "  }\n\n"
            "  Additional runtime inputs (via the action input) may include:\n"
            "    - text prompt to append to the messages\n"
            "    - urls: list of file or image URLs. Image URLs (jpeg, jpg, png, gif, webp)\n"
            "      are inserted as image parts; other file URLs may be uploaded or passed\n"
            "      to provider-specific file handling logic.\n\n"
            "Behavior:\n"
            "  - Combines configured messages and runtime input into a final chat request.\n"
            "  - Resolves and inlines tool definitions when tools are referenced by name.\n"
            "  - Supports streaming and non-streaming modes and provider-specific handling.\n\n"
            "Output:\n"
            "  A DataBlocks object where each block contains one model-generated text\n"
            "  response (the content of each choice). If an error occurs, the exception\n"
            "  is returned."
        )

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return LlmChatOpenaiConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return LlmChatOpenaiConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return LlmChatOpenaiConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    # def __init__(self, action_config: LlmChatOpenaiConfig, user: UserORM | None = None, db: AsyncIOMotorDatabase | None = None):
    #     super().__init__(action_config=action_config, user=user, db=db)

    @staticmethod
    async def update_config_with_prev_results(
        ctx: Context,
        app_state: AppState,
        curr_config: LlmChatOpenaiConfig,
        prev_results: List[ActionResult] | None = None,
    ) -> LlmChatOpenaiConfig:
        return curr_config

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        data_blocks = DataBlocks(blocks=[])
        try:
            openai_client = await AppConn.get_openai(
                ctx=ctx, db=self.db, user=self.user, provider=self.action_config.provider
            )
            if isinstance(openai_client, Exception):
                raise openai_client
            tool_defs = await ActionText2TextLlmChatOpenai.replace_action_tools_with_tools_defs(
                ctx=ctx, action_config=self.action_config
            )
            self.action_config.tools = tool_defs
            messages = []
            if action_input and action_input.urls:
                file_urls = []
                for url in action_input.urls:
                    if url.split(".")[-1].lower() in self.IMAGE_FORMATS:
                        image_message = ChatCompletionUserMessageParam(
                            role="user",
                            content=[
                                ChatCompletionContentPartImageParam(
                                    image_url=ImageURL(url=url, detail="auto"), type="image_url"
                                )
                            ],
                        )
                        messages.append(image_message)
                    else:
                        file_urls.append(url)

                # TODO: Debug getting error  Error: Error code: 500 - {'error': {'message': 'The server had an error processing your request. Sorry about that! You can retry your request, or contact us through our help center at help.openai.com if you keep seeing this error. (Please include the request ID req_761d92d7600143518e5e93aba7131c88 in your email.)', 'type': 'server_error', 'param': None, 'code': None}}
                # if file_urls:
                #     match self.action_config.provider:
                #         case "azure":
                #             pass
                #         case "openai":
                #             uploaded_files: Dict[
                #                 str, FileObject
                #             ] = await FileUploader.upload_urls_to_openai(
                #                 ctx=ctx,
                #                 user=self.user,
                #                 db=self.db,
                #                 urls=file_urls,
                #                 openai_client=openai_client,
                #                 supported_file_formats=OPENAI_FILE_FORMATS,
                #             )
                #             content = []
                #             for url, file_obj in uploaded_files.items():
                #                 file = File(type="file", file=FileFile(file_id=file_obj.id))
                #                 content.append(file)
                #             if content:
                #                 message = ChatCompletionUserMessageParam(
                #                     role="user", content=content
                #                 )
                #                 messages.append(message)

            if action_input and (action_input.text != "" or action_input.urls):
                llm_prompt = action_input.get_llm_prompt(ctx=ctx)
                logger.bind(ctx=ctx, llm_prompt=truncate_str(llm_prompt, limit=100)).info(
                    "llm prompt"
                )
                message = ChatCompletionUserMessageParam(role="user", content=llm_prompt)
                messages.append(message)

            self.action_config.messages += messages
            chat_req = ChatReq(**self.action_config.model_dump(exclude_none=True))
            chat_res = None
            # if self.action_config.stream:
            #     chat_res = await openai_client.openai_chat_stream.chat(
            #         ctx=ctx,
            #         chat_req=chat_req,
            #         user=self.user,
            #         db=self.db,
            #         notify=notify,
            #         tool_output_text_to_file_char_limit=self.action_config.tool_output_text_to_file_char_limit,
            #     )
            # else:
            chat_res = await openai_client.openai_chat.chat(
                ctx=ctx,
                app_state=app_state,
                chat_req=chat_req,
                user=self.user,
                db=self.db,
                notify=notify,
                tool_output_text_to_file_char_limit=self.action_config.tool_output_text_to_file_char_limit,
            )
            self.action_config = self.action_config.model_copy(
                update=chat_req.model_dump(exclude_none=True)
            )  # creates a new model and assigns it to self.action_config, breaking the reference to config passed in __init__
            # self.action_config.messages = chat_req.messages # This wont break the reference, but what about fields other thhan message, will have to keep track of all fields that might change
            if not chat_res:
                return data_blocks
            for choice in chat_res.choices:
                if choice.message.content:
                    data_blocks.blocks.append(DataBlock(text=choice.message.content))
                else:
                    logger.bind(ctx=ctx, choice=choice).error(
                        "openai_chat.chat response choice.message.content is None"
                    )
            if len(data_blocks.blocks) == 0:
                raise HTTPException(
                    status_code=503, detail="No response from OpenAI, please try again."
                )
            return data_blocks
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in run action of LLMChat: {str(e)}")
            return e

    async def run_tool(
        self, action_config: LlmChatOpenaiConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        data_blocks = DataBlocks(blocks=[])
        try:
            # getting tool definations for the openai chat model
            tool_defs = await ActionText2TextLlmChatOpenai.replace_action_tools_with_tools_defs(
                ctx=ctx, action_config=action_config
            )
            action_config.tools = tool_defs

            messages = action_config.messages.copy() if action_config.messages else []

            # urls also can be passed in action config now
            image_formats = self.IMAGE_FORMATS
            new_messages: list[ChatCompletionUserMessageParam] = []

            for msg in messages:
                role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
                content = (
                    msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
                )
                new_messages.append(ChatCompletionUserMessageParam(role=role, content=content))
                if isinstance(content, str):
                    block = DataBlock(
                        text=content,
                        metadata=Metadata(process_url_in_text=True),
                    )
                    block.process_url_in_text(ctx)
                    for url in block.urls:
                        ext = url.split(".")[-1].lower()
                        if ext in image_formats:
                            new_messages.append(
                                ChatCompletionUserMessageParam(
                                    role="user",
                                    content=[
                                        ChatCompletionContentPartImageParam(
                                            image_url=ImageURL(url=url, detail="auto"),
                                            type="image_url",
                                        )
                                    ],
                                )
                            )

            openai_client = await AppConn.get_openai(
                ctx=ctx, db=self.db, user=self.user, provider=action_config.provider
            )
            if isinstance(openai_client, Exception):
                raise openai_client

            # Update config to include modified messages
            action_config.messages = new_messages

            chat_req = ChatReq(**action_config.model_dump(exclude_none=True))
            chat_res = await openai_client.openai_chat.chat(
                ctx=ctx,
                app_state=app_state,
                chat_req=chat_req,
                user=self.user,
                notify=notify,
            )

            if not chat_res:
                raise AppException(detail="Cannot run LLM", http_status=500)

            for choice in chat_res.choices:
                data_blocks.blocks.append(DataBlock(text=choice.message.content))

            return data_blocks

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in run_tool of LLMChat: {str(e)}")
            return e

    @staticmethod
    async def replace_action_tools_with_tools_defs(
        ctx: Context, action_config: LlmChatOpenaiConfig
    ) -> List[ChatCompletionToolParam] | None:
        tool_defs = []
        if action_config.tools:
            action_tools = []
            for action_tool in action_config.tools:
                if isinstance(action_tool, str):
                    if action_tool in TOOLS_MAP:
                        action_tools.append(action_tool)
                if isinstance(action_tool, dict):
                    tool_defs.append(action_tool)
            new_tool_defs = await ToolFactory.get_chat_completion_tool_param(
                ctx=ctx, tool_names=action_tools
            )
            if new_tool_defs:
                tool_defs += new_tool_defs
            if len(tool_defs) == 0:
                return None
            return tool_defs
