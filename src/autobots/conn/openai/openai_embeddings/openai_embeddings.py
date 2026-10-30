from loguru import logger
from openai import AsyncAzureOpenAI, AsyncOpenAI
from openai.types import CreateEmbeddingResponse
from tenacity import retry, stop_after_attempt, wait_fixed

from src.autobots.conn.openai.openai_embeddings.embedding_model import EmbeddingReq, EmbeddingRes


class OpenaiEmbeddings:
    def __init__(self, client: AsyncAzureOpenAI | AsyncOpenAI):
        self.client = client

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(3), reraise=True)
    async def embeddings(self, embedding_req: EmbeddingReq) -> EmbeddingRes | None:
        try:
            logger.trace("Starting OpenAI Embedding")
            if "" in embedding_req.input:
                embedding_req.input = ["dummy" if x == "" else x for x in embedding_req.input]
            # TODO : check embeddings in larger number if dropped
            # for i in range(0, len(embedding_req.input), 100):
            #     if i + 100 <= len(embedding_req.input):
            #         batch = df_text.iloc[i:i+100]
            #   res: CreateEmbeddingResponse = await self.client.embeddings.create(**embedding_req.model_dump())

            #     else:
            #         batch = df_text.iloc[i:]
            # res: CreateEmbeddingResponse = await self.client.embeddings.create(**embedding_req.model_dump())

            res: CreateEmbeddingResponse = await self.client.embeddings.create(
                **embedding_req.model_dump()
            )
            logger.trace("Completed OpenAI Embedding")
            resp: EmbeddingRes = EmbeddingRes(**res.model_dump())
            return resp
        except Exception as e:
            logger.error(str(e))
            raise
