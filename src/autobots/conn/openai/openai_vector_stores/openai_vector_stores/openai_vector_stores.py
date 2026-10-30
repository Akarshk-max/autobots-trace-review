from typing import List

from loguru import logger
from openai import AsyncAzureOpenAI, AsyncOpenAI
from openai._base_client import AsyncPaginator
from openai.pagination import AsyncCursorPage, AsyncPage
from openai.types import VectorStore, VectorStoreDeleted, VectorStoreSearchResponse

from src.autobots.conn.openai.openai_vector_stores.openai_vector_stores.openai_vector_stores_model import (
    CreateVectorStore,
    DeleteVectorStore,
    ListVectorStore,
    RetrieveVectorStore,
    SearchVectorStore,
    UpdateVectorStore,
)


class OpenaiVectorStores:
    def __init__(self, openai_client: AsyncAzureOpenAI | AsyncOpenAI):
        self.client = openai_client

    async def create(self, create_vector_store: CreateVectorStore) -> VectorStore:
        try:
            vector_store = await self.client.vector_stores.create(
                **create_vector_store.model_dump(exclude_none=True)
            )
            return vector_store
        except Exception as e:
            logger.error(str(e))
            raise

    async def list(self, list_vector_store: ListVectorStore) -> List[VectorStore]:
        try:
            vector_stores_cursor: AsyncPaginator[
                VectorStore, AsyncCursorPage[VectorStore]
            ] = await self.client.vector_stores.list(
                **list_vector_store.model_dump(exclude_none=True)
            )
            # return vector_store
            vector_stores = []
            async for vector_store in vector_stores_cursor:
                if len(vector_stores) >= list_vector_store.limit:
                    break
                vector_stores.append(vector_store)
            return vector_stores
        except Exception as e:
            logger.error(str(e))
            raise

    async def retrieve(self, retrieve_vector_store: RetrieveVectorStore) -> VectorStore:
        try:
            vector_store = await self.client.vector_stores.retrieve(
                **retrieve_vector_store.model_dump(exclude_none=True)
            )
            return vector_store
        except Exception as e:
            logger.error(str(e))
            raise

    async def update(self, update_vector_store: UpdateVectorStore) -> VectorStore:
        try:
            vector_store = await self.client.vector_stores.update(
                **update_vector_store.model_dump(exclude_none=True)
            )
            return vector_store
        except Exception as e:
            logger.error(str(e))
            raise

    async def delete(self, delete_vector_store: DeleteVectorStore) -> VectorStoreDeleted:
        try:
            vector_store = await self.client.vector_stores.delete(
                **delete_vector_store.model_dump(exclude_none=True)
            )
            return vector_store
        except Exception as e:
            logger.error(str(e))
            raise

    async def search(
        self, search_vector_store: SearchVectorStore
    ) -> AsyncPaginator[VectorStoreSearchResponse, AsyncPage[VectorStoreSearchResponse]]:
        try:
            vector_store = self.client.vector_stores.search(
                **search_vector_store.model_dump(exclude_none=True)
            )
            return vector_store
        except Exception as e:
            logger.error(str(e))
            raise
