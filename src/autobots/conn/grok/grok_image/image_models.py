from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# Exceptions classes
class XAIImageError(Exception):
    """Base exception for image operations."""


class XAIImageClientError(XAIImageError):
    """Raised when client creation/usage fails."""


class XAIImageGenerateError(XAIImageError):
    """Raised when image generation fails."""


class XAIImageFetchError(XAIImageError):
    """Raised when fetching image bytes (e.g. from URL) fails."""


class XAIImageSaveError(XAIImageError):
    """Raised when saving/uploading image(s) fails."""


# Config pydantic models
ImageFormatType = Literal["base64", "url"]


class GrokImageModel(str, Enum):
    grok_imagine_image_2_0 = "grok-imagine-image-2.0"
    grok_imagine_image_quality = "grok-imagine-image-quality"
    grok_imagine_image = "grok-imagine-image"
    grok_2_image_1212 = "grok-2-image-1212"
    grok_2_image = "grok-2-image"


class ImageGenerationRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    prompt: str
    model: GrokImageModel = GrokImageModel.grok_2_image_1212
    n: int = Field(default=1, ge=1, le=10)

    image_format: ImageFormatType = "url"
    fetch_bytes_if_url: bool = True
    include_image_bytes: bool = True
    provider_url: Optional[str] = None

    save_to_s3: bool = False
    urls_only: bool = False


class GeneratedImage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    index: int
    revised_prompt: Optional[str] = None
    provider_url: Optional[str] = None
    url: Optional[str] = None
    image_bytes: Optional[bytes] = None

    content_type: Optional[str] = None
    size_bytes: Optional[int] = None


class ImageGenerationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    request: ImageGenerationRequest
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    images: list[GeneratedImage]
    warnings: list[str] = Field(default_factory=list)
