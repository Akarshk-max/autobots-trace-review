import base64
import io
import re
from enum import Enum
from typing import List, Optional, Type
from urllib.parse import urlparse

import httpx
from httpx import URL
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai.types.chat import ChatCompletionUserMessageParam
from PIL import Image
from pydantic import BaseModel

from src.autobots.action.action.action_doc_model import ActionCreate, ActionResult
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks, TextObjs
from src.autobots.action.action_type.abc.ActionABC import (
    ActionABC,
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
    ActionInputType,
    ActionOutputType,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    LlmChatOpenaiConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.aws.s3 import get_s3
from src.autobots.conn.chroma.chroma import Document
from src.autobots.conn.openai.openai_chat.chat_model import ChatReq
from src.autobots.core.utils import truncate_str
from src.autobots.data_model.context import Context
from src.autobots.datastore.multidatastore import MultiDataStore, SearchResult
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

def extract_filename(s3_url):
    # Parse the S3 URL
    parsed_url = urlparse(s3_url)
    # Extract the path from the parsed URL
    path = parsed_url.path
    # Split the path and get the last part, which is the filename
    filename = path.split("/")[-1]
    return filename


def resize_base64_image(base64_string: str, size=(128, 128)):
    """
    Resize an image encoded as a Base64 string
    """
    # Decode the Base64 string
    img_data = base64.b64decode(base64_string)
    img = Image.open(io.BytesIO(img_data))

    # Resize the image
    resized_img = img.resize(size)

    # Save the resized image to a bytes buffer
    buffered = io.BytesIO()
    resized_img.save(buffered, format=img.format)

    # Encode the resized image to Base64
    return base64.b64encode(buffered.getvalue()).decode("utf-8")


def looks_like_base64(sb: str):
    """Check if the string looks like base64"""
    return re.match("^[A-Za-z0-9+/]+[=]{0,2}$", sb) is not None


def is_image_data(b64data: str):
    """
    Check if the base64 data is an image by looking at the start of the data
    """
    image_signatures = {
        b"\xff\xd8\xff": "jpg",
        b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a": "png",
        b"\x47\x49\x46\x38": "gif",
        b"\x52\x49\x46\x46": "webp",
    }
    try:
        header = base64.b64decode(b64data)[:8]  # Decode and get the first 8 bytes
        for sig, format in image_signatures.items():
            if header.startswith(sig):
                return True
        return False
    except Exception as e:
        logger.exception(f"Error in is_image_data: {str(e)}")
        return False


def split_image_text_types(docs: List[SearchResult]):
    """
    Split base64-encoded images and texts
    """
    b64_images = []
    texts = []
    for doc in docs:
        # Check if the document is of type Document and extract page_content if so
        if isinstance(doc, SearchResult):
            data = doc.data
            if looks_like_base64(data) and is_image_data(data):
                data = resize_base64_image(data, size=(1300, 600))
                b64_images.append(data)
            else:
                texts.append(data)
            continue

        if isinstance(doc, Document):
            doc = doc.page_content
        if looks_like_base64(doc) and is_image_data(doc):
            doc = resize_base64_image(doc, size=(1300, 600))
            b64_images.append(doc)
        else:
            texts.append(doc)
    return {"images": b64_images, "texts": texts}


def img_prompt_func(data_dict):
    """
    Join the context into a single string
    """
    formatted_texts = "\n".join(data_dict["texts"])
    messages = []

    # Adding image(s) to the messages if present
    if data_dict["images"]:
        for image in data_dict["images"]:
            image_message = {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image}"},
            }
            messages.append(image_message)

    # Adding the text for analysis
    text_message = {
        "type": "text",
        "text": (
            "You are a helpful assistant, answer the questions from the data given to you\n"
            f"User-provided question: {data_dict['question']}\n\n"
            "Text and / or tables with sources:\n"
            f"{formatted_texts}"
        ),
    }
    messages.append(text_message)
    return messages


class Operations(str, Enum):
    put_resource = "put_resource"
    search = "search"


class ActionCreateMultiModalLlmChatWithVectorSearchOpenaiConfig(BaseModel):
    datastore_id: str
    operations: Operations
    chat_req: LlmChatOpenaiConfig | None = None
    input: Optional[str] = None
    output: Optional[TextObjs] = None
    urls: Optional[List[str]] = None


class ActionCreateMultiModalLlmChatWithVectorSearchOpenai(ActionCreate):
    type: ActionType = ActionType.multimodal_llm_chat_with_vector_search_openai
    config: ActionCreateMultiModalLlmChatWithVectorSearchOpenaiConfig


class ActionMultiModalLlmChatWithVectorSearchOpenai(
    ActionABC[
        ActionCreateMultiModalLlmChatWithVectorSearchOpenaiConfig,
        ActionCreateMultiModalLlmChatWithVectorSearchOpenaiConfig,
        ActionCreateMultiModalLlmChatWithVectorSearchOpenaiConfig,
        DataBlock,
        DataBlocks,
    ]
):
    """
    Vector search and add it to chat prompt as context
    """

    type = ActionType.multimodal_llm_chat_with_vector_search_openai

    @staticmethod
    def get_description() -> str:
        return """
Description:
- A multimodal vector-search and retrieval-augmented generation (RAG) tool with optional image support.
- This tool either:
  1) Adds new resources into the vector datastore, or
  2) Performs a vector similarity search and returns the retrieved context as text.

Supported operations:
- "put_resource": Download and store external files (from URLs) into the datastore so they can be searched later.
- "search": Query the vector datastore with a user-provided text input and return relevant stored results.

When to use this tool:
- Use "put_resource" when ingesting external data sources into your datastore before running AI queries.
- Use "search" when you want contextual information past data you have stored in vector search backend, including possible embedded images encoded into text results.

Input config:
{
  "datastore_id": "<id of vector datastore>",
  "operations": "search" | "put_resource",
  "input": "<query text for vector search>",         # required for search
  "urls": ["https://example.com/file1.pdf", ...]     # required for put_resource
}

Fields:
- datastore_id (string, required):
    - Identifies which vector store to operate on.
- operations (string, required):
    - Allowed values:
        - "search" to retrieve matching context
        - "put_resource" to insert new content
- input (string, optional):
    - Required when operations="search"
    - Acts as the search query text.
- urls (list of strings, optional):
    - Required when operations="put_resource"
    - Each URL is downloaded and added as a new document in the datastore.

Output:
- When operations="search":
  A DataBlocks response:
  {
    "blocks": [
      { "text": "<retrieved context 1>" },
      { "text": "<retrieved context 2>" },
      ...
    ]
  }

- When operations="put_resource":
  {
    "blocks": [
      { "text": "Success" }
    ]
  }

"""

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return ActionCreateMultiModalLlmChatWithVectorSearchOpenaiConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return ActionCreateMultiModalLlmChatWithVectorSearchOpenaiConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return ActionCreateMultiModalLlmChatWithVectorSearchOpenaiConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    def __init__(
        self,
        action_config: ActionCreateMultiModalLlmChatWithVectorSearchOpenaiConfig,
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase | None = None,
    ):
        super().__init__(action_config=action_config, user=user, db=db)
        self.datastore = MultiDataStore(
            s3=get_s3(), openai_provider=action_config.chat_req.provider
        ).hydrate(datastore_id=action_config.datastore_id)

    @staticmethod
    async def update_config_with_prev_results(
        ctx: Context,
        app_state: AppState,
        curr_config: ActionCreateMultiModalLlmChatWithVectorSearchOpenaiConfig,
        prev_results: List[ActionResult] | None = None,
    ) -> ActionCreateMultiModalLlmChatWithVectorSearchOpenaiConfig:
        # if not prev_results:
        #     return curr_config
        #
        # for prev_result in prev_results:
        #     action_input: TextObj = ActionMultiModalLlmChatWithVectorSearchOpenai.get_input_type().model_validate(prev_result.input)
        #     action_output: TextObjs = ActionMultiModalLlmChatWithVectorSearchOpenai.get_output_type().model_validate(prev_result.output)
        #     config_message_1 = [ChatCompletionUserMessageParam(role="user", content=action_input.text)]
        #     config_messages_2 = [
        #         ChatCompletionAssistantMessageParam(role="assistant", content=prev_output_text_obj.text) for
        #         prev_output_text_obj in action_output.texts]
        #
        #     curr_config.chat_req.messages = curr_config.chat_req.messages + config_message_1 + config_messages_2
        return curr_config

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            # vector search
            logger.bind(ctx=ctx).info(
                f"run_action search input: '{truncate_str(action_input.text)}'"
            )
            logger.bind(ctx=ctx).info(f"run_action datastore_id: {self.datastore.id}")

            search_results = await self.datastore.search(action_input.text)
            logger.bind(ctx=ctx).info(f"run_action search results count: {len(search_results)}")

            if not search_results:
                logger.bind(ctx=ctx).warning("run_action: Search returned no results")
                return DataBlocks(blocks=[DataBlock(text="No search results found")])

            client = await AppConn.get_openai(
                ctx=ctx, db=self.db, user=self.user, provider=self.action_config.chat_req.provider
            )

            # if len(search_results) == 0:
            #     return None
            # context = "Only use relevant context to give response. If the context is insufficient say \"Cannot answer from given context\"\nContext: \n"
            # for result in search_results:
            #     context = f"{context}{result}\n"
            # LM chat
            processed_context = split_image_text_types(search_results)
            processed_context["question"] = action_input.text
            prompt = img_prompt_func(processed_context)
            # prompt = [
            #     {"role": "user", "content": prompt}
            # ]
            input_message = ChatCompletionUserMessageParam(role="user", content=prompt)
            messages = [input_message]
            chat_res = await client.openai_chat.chat(
                chat_req=ChatReq(messages=messages, model="gpt-4o-mini"), app_state=app_state, notify=notify
            )
            action_results = DataBlocks()
            for choice in chat_res.choices:
                action_results.blocks.append(DataBlock(text=choice.message.content))
            return action_results
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    async def run_tool(
        self,
        action_config: ActionCreateMultiModalLlmChatWithVectorSearchOpenaiConfig,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        if action_config.operations == "search":
            try:
                # vector search
                logger.bind(ctx=ctx).info(
                    f"run_tool search input: '{truncate_str(action_config.input)}'"
                )
                logger.bind(ctx=ctx).info(f"run_tool datastore_id: {action_config.datastore_id}")
                if not action_config.input:
                    logger.bind(ctx=ctx).warning("No input provided for search in run_tool")
                    return DataBlocks(blocks=[DataBlock(text="No input provided for search")])

                # Check if datastore is properly initialized
                logger.bind(ctx=ctx).info(f"Datastore ID: {self.datastore.id}")

                search_results = await self.datastore.search(action_config.input)
                logger.bind(ctx=ctx).info(f"run_tool search results count: {len(search_results)}")

                if not search_results:
                    logger.bind(ctx=ctx).warning("Search returned no results")
                    return DataBlocks(blocks=[DataBlock(text="No search results found")])

                # client = OpenAI(api_key=SettingsProvider.sget().OPENAI_API_KEY)

                # if len(search_results) == 0:
                #     return None
                # context = "Only use relevant context to give response. If the context is insufficient say \"Cannot answer from given context\"\nContext: \n"
                # for result in search_results:
                #     context = f"{context}{result}\n"
                # LM chat
                # processed_context = split_image_text_types(search_results)
                # processed_context['question'] = action_config.input
                # prompt = img_prompt_func(processed_context)
                # prompt = [
                #     {"role": "user", "content": prompt}
                # ]
                # chat_res = client.chat.completions.create(model="gpt-4o",
                #                                         messages=prompt)
                # message = ChatCompletionUserMessageParam(role=Role.user.value, content=f"{context}Question: {action_input.text}")
                # self.action_config.chat_req.messages = self.action_config.chat_req.messages + [message]
                # self.action_config.input = action_input
                # # chat_res = await get_openai().openai_chat.chat(chat_req=self.action_config.chat_req)
                #     action_results.texts.append(TextObj(text=choice.message.content))
                action_results = DataBlocks()
                for result in search_results:
                    action_results.blocks.append(DataBlock(text=result.data))
                return action_results
            except Exception as e:
                logger.bind(ctx=ctx).exception(e)
                return e
        if action_config.operations == "put_resource":
            # Download file in temp and extract filename from the url
            with httpx.Client() as client:
                for url in action_config.urls:
                    response = client.get(url=URL(url))
                    await self.datastore.put_data(
                        ctx,
                        user=self.user,
                        db=self.db,
                        file=(extract_filename(url), response.content),
                    )
            action_results = DataBlocks()
            action_results.blocks.append(DataBlock(text="Success"))
            return action_results
