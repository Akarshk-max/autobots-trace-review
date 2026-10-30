from typing import List, Optional

from pydantic import BaseModel, Field, HttpUrl

from src.autobots.conn.unstructured_io.unstructured_io import PartitionParametersParams


class FileMeta(BaseModel):
    name: str
    url: HttpUrl
    # Storage source metadata (all optional for backward compatibility).
    # Used by Daytona input presigning to decide whether/how to authorize downloads.
    storage_scope: Optional[str] = None  # "meetkiwi_public" | "meetkiwi_private" | "customer_s3"
    bucket: Optional[str] = None
    key: Optional[str] = None
    # Only set for customer_s3; identifies which named AWS connection owns the object.
    connection_name: Optional[str] = None


class FileMetaPlus(FileMeta):
    original_filename: str


class FileAndProcessingParams(BaseModel):
    filename: str
    processing_params: PartitionParametersParams


class FilesAndProcessingParams(BaseModel):
    files_and_params: List[FileAndProcessingParams]


class ResolvedDownloadUrl(BaseModel):
    """Result of resolving one file URL into a downloadable URL."""

    original_url: HttpUrl
    # Signed URL for private objects; identical to original_url when unsigned.
    url: HttpUrl
    signed: bool


class ResolveDownloadUrlsRequest(BaseModel):
    urls: List[HttpUrl] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="File URLs to resolve into downloadable URLs.",
    )
    expiration_sec: int = Field(
        default=24 * 60 * 60,
        ge=60,
        le=7 * 24 * 60 * 60,
        description="Presigned URL lifetime in seconds. Defaults to 1 day; max 7 days.",
    )
