import hashlib
import re
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# Exceptions defined for using in collection class
class XAICollectionError(Exception):
    """Base exception for collection operations."""


class XAICreateCollectionError(XAICollectionError):
    """Raised when collection creation fails."""


class XAIListCollectionsError(XAICollectionError):
    """Raised when listing collections fails."""


class XAIGetCollectionError(XAICollectionError):
    """Raised when fetching collection metadata fails."""


class XAIUpdateCollectionError(XAICollectionError):
    """Raised when updating a collection fails."""


class XAIDeleteCollectionError(XAICollectionError):
    """Raised when deleting a collection fails."""


class XAIUploadDocumentError(XAICollectionError):
    """Raised when uploading a document fails."""


class XAIAddExistingDocumentError(XAICollectionError):
    """Raised when adding an existing file to a collection fails."""


class XAIListDocumentsError(XAICollectionError):
    """Raised when listing documents in a collection fails."""


class XAIGetDocumentError(XAICollectionError):
    """Raised when fetching document metadata fails."""


class XAIBatchGetDocumentsError(XAICollectionError):
    """Raised when batch fetching document metadata fails."""


class XAIUpdateDocumentError(XAICollectionError):
    """Raised when updating a document fails."""


class XAISearchError(XAICollectionError):
    """Raised when searching collections fails."""


class XAIReindexError(XAICollectionError):
    """Raised when reindexing a document fails."""


class XAIDocumentRemovalError(XAICollectionError):
    """Raised when removing a document from a collection fails."""


class XAIDownloadError(XAICollectionError):
    """Raised when downloading from a URL fails (https/s3)."""


class XAIDuplicateDocumentError(RuntimeError):
    """Raised when collection unique constraint blocks a duplicate upload."""

    pass


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _looks_like_duplicate_exc(exc: BaseException) -> bool:
    s = str(exc).lower()
    if "unique" in s and (
        "violation" in s or "constraint" in s or "duplicate" in s or "already" in s
    ):
        return True
    if re.search(r"\b409\b", s):
        return True

    return False


# Pydantic models for collection classes
class TokensChunkConfiguration(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    max_chunk_size_tokens: int
    chunk_overlap_tokens: int
    encoding_name: Optional[str] = None


class CharsChunkConfiguration(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    max_chunk_size_chars: int
    chunk_overlap_chars: int


class ChunkConfiguration(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tokens_configuration: Optional[TokensChunkConfiguration] = None
    chars_configuration: Optional[CharsChunkConfiguration] = None
    strip_whitespace: Optional[bool] = None


class IndexConfiguration(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    model_name: Optional[str] = None


class FieldDefinition(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    required: bool = False
    unique: bool = False


class CollectionMetadata(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    collection_id: str
    collection_name: str
    documents_count: int = 0

    created_at: Optional[datetime] = None
    index_configuration: Optional[IndexConfiguration] = None
    chunk_configuration: Optional[ChunkConfiguration] = None
    field_definitions: Optional[list[FieldDefinition]] = None


class ListCollectionsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    collections: list[CollectionMetadata]
    pagination_token: Optional[str] = None


class DocumentFileMetadata(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    file_id: str
    name: str
    size_bytes: int = 0
    content_type: Optional[str] = None


class DocumentMetadata(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    file_metadata: DocumentFileMetadata
    status: Optional[str] = None
    fields: dict[str, Any] = Field(default_factory=dict)


class ListDocumentsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    documents: list[DocumentMetadata]
    pagination_token: Optional[str] = None


class BatchGetDocumentsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    documents: list[DocumentMetadata]


class SearchMatch(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    file_id: str
    score: float
    chunk_content: str


class SearchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    matches: list[SearchMatch]
