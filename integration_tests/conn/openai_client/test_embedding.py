from typing import Literal

import pytest

from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.openai.openai_embeddings.embedding_model import EmbeddingReq, EmbeddingRes


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_embedding_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    texts = ["Hello", "World"]
    embedding_req = EmbeddingReq(input=texts)

    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=provider)
    resp: EmbeddingRes = await client.openai_embeddings.embeddings(embedding_req=embedding_req)

    assert len(resp.data) == 2
    assert len(resp.data[0].embedding) == 1536
    assert len(resp.data[1].embedding) == 1536
