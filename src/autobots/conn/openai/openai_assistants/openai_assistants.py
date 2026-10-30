import uuid

from loguru import logger
from openai import AsyncAzureOpenAI, AsyncOpenAI
from openai._base_client import AsyncPaginator
from openai._legacy_response import HttpxBinaryResponseContent
from openai.pagination import AsyncCursorPage
from openai.types import FileObject
from openai.types.beta import Assistant, AssistantDeleted

from src.autobots.conn.openai.openai_assistants.assistant_model import (
    AssistantCreate,
    AssistantDelete,
    AssistantList,
    AssistantRetrieve,
    AssistantUpdate,
)
from src.autobots.conn.openai.openai_assistants.openai_threads.openai_threads import OpenaiThreads


class OpenaiAssistants:
    def __init__(self, openai_client: AsyncAzureOpenAI | AsyncOpenAI):
        self.client = openai_client
        self.threads = OpenaiThreads(openai_client)

    async def create(self, assistant_create: AssistantCreate) -> Assistant | None:
        try:
            random_assistant_id = str(uuid.uuid4())
            assistant_name = f"{random_assistant_id}"
            assistant = await self.client.beta.assistants.create(
                name=assistant_name, **assistant_create.model_dump(exclude_none=True)
            )
            return assistant
        except Exception as e:
            logger.exception(str(e))
            raise

    async def retrieve(self, assistant_retrieve: AssistantRetrieve) -> Assistant | None:
        try:
            return await self.client.beta.assistants.retrieve(
                **assistant_retrieve.model_dump(exclude_unset=True)
            )
        except Exception as e:
            logger.exception(str(e))
            raise

    async def list(
        self, assistant_list: AssistantList
    ) -> AsyncPaginator[Assistant, AsyncCursorPage[Assistant]]:
        try:
            return await self.client.beta.assistants.list(
                **assistant_list.model_dump(exclude_none=True)
            )
        except Exception as e:
            logger.exception(str(e))
            raise

    async def delete(self, assistant_delete: AssistantDelete) -> AssistantDeleted | None:
        try:
            return await self.client.beta.assistants.delete(
                **assistant_delete.model_dump(exclude_none=True)
            )
        except Exception as e:
            logger.exception(str(e))
            raise

    async def update(self, assistant_update: AssistantUpdate) -> Assistant | None:
        try:
            # Separate the assistant_id (a path parameter) from the update payload (the request body).
            # This is the critical fix for the signature mismatch with the OpenAI SDK.
            payload = assistant_update.model_dump(
                exclude_unset=True,
                exclude={"assistant_id"},  # The ID does not go in the body.
            )

            return await self.client.beta.assistants.update(
                assistant_update.assistant_id,  # Pass assistant_id as a positional argument.
                **payload,  # Unpack the rest of the body as keyword arguments.
            )
        except Exception as e:
            logger.exception(str(e))
            raise

    async def file_retrieve(self, file_id: str) -> FileObject:
        try:
            file_obj = await self.client.files.retrieve(file_id=file_id)
            return file_obj
        except Exception as e:
            logger.exception(str(e))
            raise

    async def file_retrieve_content(self, file_id: str) -> bytes:
        try:
            httpx_res: HttpxBinaryResponseContent = await self.client.files.content(file_id=file_id)
            res_bytes = httpx_res.content
            return res_bytes
        except Exception as e:
            logger.exception(str(e))
            raise
