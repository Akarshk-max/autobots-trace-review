from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class XAIVideoError(Exception):
    """Base exception for video operations."""


class XAIVideoClientError(XAIVideoError):
    """Raised when client creation/usage fails."""


class XAIVideoGenerateError(XAIVideoError):
    """Raised when video generation fails."""


class XAIVideoFetchError(XAIVideoError):
    """Raised when fetching video file from provider URL fails."""


class XAIVideoSaveError(XAIVideoError):
    """Raised when saving/uploading generated video(s) fails."""


VideoAspectRatioType = Literal["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"]
VideoResolutionType = Literal["480p", "720p"]


class GrokVideoModel(str, Enum):
    grok_imagine_video_1_5 = "grok-imagine-video-1.5"
    grok_imagine_video = "grok-imagine-video"


class VideoGenerationRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    prompt: str
    model: GrokVideoModel = GrokVideoModel.grok_imagine_video
    duration: Optional[int] = Field(default=None, ge=1, le=15)

    aspect_ratio: VideoAspectRatioType | None = None
    resolution: VideoResolutionType | None = None
    image_url: Optional[str] = None
    video_url: Optional[str] = None

    poll_timeout_sec: Optional[float] = Field(default=None, gt=0)
    poll_interval_sec: Optional[float] = Field(default=None, gt=0)


class GeneratedVideo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    index: int
    revised_prompt: Optional[str] = None
    url: str

    content_type: Optional[str] = None
    size_bytes: Optional[int] = None

    duration_sec: Optional[int] = None
    respect_moderation: Optional[bool] = None
    model: Optional[str] = None


class VideoGenerationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    request: VideoGenerationRequest
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    videos: list[GeneratedVideo]
    warnings: list[str] = Field(default_factory=list)