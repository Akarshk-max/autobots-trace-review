from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


# all the exception class is defined here
class XAIFileError(Exception):
    """Base exception for file operations."""


class XAIUploadError(XAIFileError):
    """Raised when file upload fails."""


class XAIDownloadError(XAIFileError):
    """Raised when file download fails."""


class XAIListError(XAIFileError):
    """Raised when listing files fails."""


class XAIGetError(XAIFileError):
    """Raised when fetching file metadata fails."""


class XAIDeleteError(XAIFileError):
    """Raised when deleting a file fails."""


# all the pydantic model for xai files handling classes
class FileMetadata(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    size: int
    team_id: Optional[str] = None
    created_at: datetime
    expires_at: Optional[datetime] = None


class DeleteResponse(BaseModel):
    id: str
    deleted: bool


class ListFilesResponse(BaseModel):
    files: list[FileMetadata]
    pagination_token: Optional[str] = None
