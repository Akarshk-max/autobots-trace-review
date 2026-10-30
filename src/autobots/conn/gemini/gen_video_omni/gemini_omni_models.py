from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class OmniVideoModel(str, Enum):
    OMNI_1_1_FLASH = "gemini-omni-1.1-flash"


class OmniAspectRatio(str, Enum):
    R_16_9 = "16:9"
    R_9_16 = "9:16"


class OmniResolution(str, Enum):
    P360 = "360p"
    P720 = "720p"
    P1080 = "1080p"
    K4 = "4k"


class OmniVideoTask(str, Enum):
    TEXT_TO_VIDEO = "text_to_video"
    IMAGE_TO_VIDEO = "image_to_video"
    REFERENCE_TO_VIDEO = "reference_to_video"
    EDIT = "edit"
    EXTEND = "extend"


class OmniErrorPayload(BaseModel):
    code: str
    message: str
    retryable: bool = False
    provider: Literal["gemini"] = "gemini"
    http_status: Optional[int] = None
    details: Optional[Dict[str, Any]] = None


class OmniVideoException(Exception):
    def __init__(self, payload: OmniErrorPayload):
        super().__init__(payload.message)
        self.payload = payload


def is_retryable_exception(e: Exception) -> bool:
    msg = str(e).lower()
    if "timeout" in msg or "timed out" in msg:
        return True
    if "connection" in msg and ("reset" in msg or "refused" in msg):
        return True
    if "rate limit" in msg or "429" in msg:
        return True
    if "temporarily" in msg or "unavailable" in msg or "deadline" in msg:
        return True
    return False


class OmniMediaInput(BaseModel):
    url: Optional[str] = None
    file_path: Optional[str] = None
    base64_content: Optional[str] = None
    gemini_uri: Optional[str] = None

    file_name: Optional[str] = None
    mime_type: Optional[str] = None

    @model_validator(mode="after")
    def validate_one_source(self):
        sources = [self.url, self.file_path, self.base64_content, self.gemini_uri]
        if sum(bool(s) for s in sources) != 1:
            raise ValueError("Provide exactly one of: url, file_path, base64_content, gemini_uri")
        return self


class OmniVideoGenerateConfig(BaseModel):
    aspect_ratio: Optional[OmniAspectRatio] = None
    resolution: Optional[OmniResolution] = None
    duration_seconds: Optional[int] = Field(default=None, ge=3, le=10)
    task: Optional[OmniVideoTask] = None

    request_timeout_seconds: float = Field(default=900.0, ge=30.0, le=3600.0)


class OmniVideoRequest(BaseModel):
    model: OmniVideoModel = OmniVideoModel.OMNI_1_1_FLASH
    prompt: str = Field(..., min_length=1)

    image: Optional[OmniMediaInput] = None
    video: Optional[OmniMediaInput] = None
    previous_interaction_id: Optional[str] = None

    generation_config: OmniVideoGenerateConfig = Field(default_factory=OmniVideoGenerateConfig)

    upload_to_s3: bool = True
    s3_prefix: str = "omni-videos"
    fail_on_s3_error: bool = True


class OmniGeneratedVideo(BaseModel):
    mime_type: str
    file_name: str
    provider_uri: Optional[str] = None
    s3_url: Optional[str] = None


class OmniVideoResult(BaseModel):
    model: str
    interaction_id: Optional[str] = None
    videos: List[OmniGeneratedVideo] = Field(default_factory=list)
    s3_urls: List[str] = Field(default_factory=list)
    provider_uris: List[str] = Field(default_factory=list)
