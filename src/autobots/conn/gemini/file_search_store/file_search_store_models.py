from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.autobots.conn.gemini.chat_model_gemini import GeminiContextStoreRef

GeminiFileSearchStoreOperation = Literal[
    "create_store",
    "get_store",
    "list_stores",
    "ingest_files",
    "reindex_documents",
    "delete_documents",
    "delete_store",
    "query_store",
    "stats",
]
GeminiFileSearchStoreScope = Literal["chat_scoped", "named_general_store"]


class GeminiFileSearchStoreRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: GeminiFileSearchStoreScope = "named_general_store"
    store_name: str | None = None
    display_name: str | None = None
    chat_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GeminiFileSearchInputFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    gemini_file_name: str | None = None
    display_name: str | None = None
    mime_type: str | None = None
    custom_metadata: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "GeminiFileSearchInputFile":
        if not self.url and not self.gemini_file_name:
            raise ValueError("Either url or gemini_file_name is required")
        return self


class GeminiFileSearchStoreReq(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: GeminiFileSearchStoreOperation
    store_ref: GeminiFileSearchStoreRef | dict[str, Any] | None = None
    display_name: str | None = None
    embedding_model: str = "models/gemini-embedding-2"

    files: list[GeminiFileSearchInputFile | dict[str, Any]] = Field(default_factory=list)
    document_names: list[str] = Field(default_factory=list)

    query: str | None = None
    top_k: int = Field(default=8, ge=1, le=20)
    metadata_filter: str | None = None
    force: bool = False

    @model_validator(mode="after")
    def validate_request(self) -> "GeminiFileSearchStoreReq":
        if self.operation in {"get_store", "delete_store", "query_store", "stats", "delete_documents", "reindex_documents", "ingest_files"}:
            if self.store_ref is None:
                raise ValueError("store_ref is required for this operation")

        if self.operation == "ingest_files" and not self.files:
            raise ValueError("files are required for ingest_files")

        if self.operation == "reindex_documents" and not self.files and not self.document_names:
            raise ValueError("reindex_documents requires files and/or document_names")

        if self.operation == "delete_documents" and not self.document_names:
            raise ValueError("document_names are required for delete_documents")

        if self.operation == "query_store" and not self.query:
            raise ValueError("query is required for query_store")

        return self

    def normalized_store_ref(self) -> GeminiFileSearchStoreRef | None:
        if self.store_ref is None:
            return None
        if isinstance(self.store_ref, GeminiFileSearchStoreRef):
            return self.store_ref
        return GeminiFileSearchStoreRef.model_validate(self.store_ref)


class GeminiFileSearchStoreDocumentRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_name: str | None = None
    display_name: str | None = None
    source_url: str | None = None
    gemini_file_name: str | None = None
    status: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GeminiFileSearchStoreRes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    operation: GeminiFileSearchStoreOperation
    summary: str | None = None
    store: dict[str, Any] | None = None
    stores: list[dict[str, Any]] = Field(default_factory=list)
    documents: list[GeminiFileSearchStoreDocumentRef | dict[str, Any]] = Field(default_factory=list)
    query_text: str | None = None
    query_response_text: str | None = None
    total_count: int = 0
    returned_count: int = 0
    failed_items: list[dict[str, Any]] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


def context_store_ref_to_file_store_ref(
    context_ref: GeminiContextStoreRef | dict[str, Any] | None,
) -> GeminiFileSearchStoreRef | None:
    if context_ref is None:
        return None
    if isinstance(context_ref, GeminiContextStoreRef):
        return GeminiFileSearchStoreRef(
            scope=context_ref.scope,
            store_name=context_ref.store_name,
            display_name=context_ref.display_name,
            chat_id=context_ref.chat_id,
            metadata=context_ref.metadata,
        )
    return GeminiFileSearchStoreRef.model_validate(context_ref)
