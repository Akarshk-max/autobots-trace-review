from typing import List, Literal, Optional, Union

from openai.types import (
    FileChunkingStrategyParam,
    Metadata,
    vector_store_create_params,
    vector_store_search_params,
)
from pydantic import BaseModel, Field


class CreateVectorStore(BaseModel):
    chunking_strategy: FileChunkingStrategyParam | None = None
    expires_after: vector_store_create_params.ExpiresAfter | None = None
    file_ids: List[str] | None = None
    metadata: Optional[Metadata] = None
    name: str | None = None


class ListVectorStore(BaseModel):
    limit: int | None = Field(None, ge=1, le=100)
    order: Literal["desc", "asc"] | None = Field(None)
    after: str | None = None
    before: str | None = None


class RetrieveVectorStore(BaseModel):
    vector_store_id: str


class UpdateVectorStore(BaseModel):
    vector_store_id: str
    name: str | None = None
    after: str | None = None
    before: str | None = None


class DeleteVectorStore(BaseModel):
    vector_store_id: str


class SearchVectorStore(BaseModel):
    vector_store_id: str
    query: Union[str, List[str]]
    filters: vector_store_search_params.Filters | None = None
    max_num_results: int | None = None
    ranking_options: vector_store_search_params.RankingOptions | None = None
    rewrite_query: bool | None = None
