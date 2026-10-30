from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from loguru import logger
from pydantic import BaseModel, Field, field_validator, model_validator


class VeoModel(str, Enum):
    VEO_3_1_GENERATE_PREVIEW = "veo-3.1-generate-preview"
    VEO_3_1_GENERATE_FAST_PREVIEW = "veo-3.1-fast-generate-preview"
    VEO_2_0_GENERATE_001 = "veo-2.0-generate-001"


class VideoAspectRatio(str, Enum):
    R_16_9 = "16:9"
    R_9_16 = "9:16"


class VideoResolution(str, Enum):
    P720 = "720p"
    P1080 = "1080p"


class VideoCompressionQuality(str, Enum):
    LOSSLESS = "LOSSLESS"
    OPTIMIZED = "OPTIMIZED"


class PersonGeneration(str, Enum):
    ALLOW_ALL = "ALLOW_ALL"
    ALLOW_ADULT = "ALLOW_ADULT"
    ALLOW_NONE = "ALLOW_NONE"


class VideoMaskMode(str, Enum):
    INSERT = "INSERT"
    OUTPAINT = "OUTPAINT"
    REMOVE = "REMOVE"
    REMOVE_STATIC = "REMOVE_STATIC"


class VideoReferenceType(str, Enum):
    REFERENCE_TYPE_ASSET = "REFERENCE_TYPE_ASSET"
    REFERENCE_TYPE_STYLE = "REFERENCE_TYPE_STYLE"


class VeoErrorPayload(BaseModel):
    code: str = Field(..., description="Stable internal error code")
    message: str
    retryable: bool = False
    provider: Literal["gemini"] = "gemini"
    http_status: Optional[int] = None
    details: Optional[Dict[str, Any]] = None


class VeoException(Exception):
    def __init__(self, payload: VeoErrorPayload):
        super().__init__(payload.message)
        self.payload = payload


def _is_retryable_exception(e: Exception) -> bool:
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

def supports_enhance_prompt(model: VeoModel) -> bool:
    # Veo 3 and 3.1: enhancePrompt is not supported
    if model == VeoModel.VEO_3_1_GENERATE_PREVIEW :
        return False
    if model == VeoModel.VEO_3_1_GENERATE_FAST_PREVIEW :
        return False

    # Veo 2.0: treat as supported
    if model == VeoModel.VEO_2_0_GENERATE_001:
        return True

    return False

class VeoMediaInput(BaseModel):
    url: Optional[str] = None
    file_path: Optional[str] = None
    base64_content: Optional[str] = None
    gemini_uri: Optional[str] = None

    file_name: Optional[str] = None
    mime_type: Optional[str] = None

    @model_validator(mode="after")
    def validate_one_source(self):
        sources = [bool(self.url), bool(self.file_path), bool(self.base64_content), bool(self.gemini_uri)]
        if sum(sources) != 1:
            raise ValueError("Provide exactly one of: url, file_path, base64_content, gemini_uri")
        return self


class VeoReferenceImage(BaseModel):
    reference_id: int = Field(..., ge=1)
    image: VeoMediaInput
    reference_type: VideoReferenceType = VideoReferenceType.REFERENCE_TYPE_ASSET

    @model_validator(mode="after")
    def validate_image_only(self):
        if self.image.gemini_uri:
            raise ValueError("reference image must be bytes-backed (url/file_path/base64_content), not gemini_uri")
        return self


class VeoMaskConfig(BaseModel):
    mask_mode: VideoMaskMode
    mask_image: VeoMediaInput

    @model_validator(mode="after")
    def validate_mask_image_only(self):
        if self.mask_image.gemini_uri:
            raise ValueError("mask_image must be bytes-backed (url/file_path/base64_content), not gemini_uri")
        return self


class VeoOutputConfig(BaseModel):
    return_bytes_b64: bool = False

    @field_validator("return_bytes_b64")
    @classmethod
    def force_false(cls, v):
        if v is True:
            raise ValueError("return_bytes_b64 must be False (S3-only)")
        return False


class VeoGenerateConfig(BaseModel):
    negative_prompt: Optional[str] = None
    enhance_prompt: Optional[bool] = None

    number_of_videos: Optional[int] = Field(default=1, ge=1, le=4)
    duration_seconds: Optional[int] = Field(default=None, ge=1, le=60)
    fps: Optional[int] = Field(default=None, ge=1, le=60)
    # seed: Optional[int] = 50

    aspect_ratio: Optional[VideoAspectRatio] = None
    resolution: Optional[VideoResolution] = None
    compression_quality: Optional[VideoCompressionQuality] = None
    person_generation: Optional[PersonGeneration] = None
    generate_audio: Optional[bool] = None

    last_frame: Optional[VeoMediaInput] = None
    mask: Optional[VeoMaskConfig] = None
    reference_images: Optional[List[VeoReferenceImage]] = None

    output_gcs_uri: Optional[str] = None
    pubsub_topic: Optional[str] = None

    output_config: VeoOutputConfig = Field(default_factory=VeoOutputConfig)

    poll_interval_seconds: float = Field(default=5.0, ge=0.5, le=60.0)
    poll_timeout_seconds: float = Field(default=900.0, ge=30.0, le=7200.0)

    @model_validator(mode="after")
    def validate_refs(self):
        if self.reference_images:
            ids = [r.reference_id for r in self.reference_images]
            if len(ids) != len(set(ids)):
                raise ValueError("reference_images.reference_id must be unique")
        if self.last_frame and self.last_frame.gemini_uri:
            raise ValueError("last_frame must be bytes-backed (url/file_path/base64_content), not gemini_uri")
        return self


class VeoVideoRequest(BaseModel):
    model: VeoModel = VeoModel.VEO_3_1_GENERATE_PREVIEW
    prompt: str = Field(..., min_length=1)

    image: Optional[VeoMediaInput] = None
    video: Optional[VeoMediaInput] = None

    generation_config: VeoGenerateConfig = Field(default_factory=VeoGenerateConfig)

    upload_to_s3: bool = True
    s3_prefix: str = "veo-videos"
    fail_on_s3_error: bool = True

    @model_validator(mode="after")
    def normalize_inputs(self):
        #  image handling 
        if self.image:
            if self.image.gemini_uri:
                logger.warning(
                    "image.gemini_uri not supported; dropping image input"
                )
                self.image = None

        #  video handling 
        if self.video:
            has_source = any(
                [
                    self.video.gemini_uri,
                    self.video.url,
                    self.video.file_path,
                    self.video.base64_content,
                ]
            )
            if not has_source:
                logger.warning(
                    "video provided without a valid source; dropping video input"
                )
                self.video = None

        #  reference images limits 
        refs = self.generation_config.reference_images
        if refs:
            if self.model in (
                VeoModel.VEO_3_1_GENERATE_PREVIEW,
                VeoModel.VEO_3_1_GENERATE_FAST_PREVIEW,
            ):
                if len(refs) > 3:
                    logger.warning(
                        "Veo 3.1 supports up to 3 reference_images; truncating from {}",
                        len(refs),
                    )
                    self.generation_config.reference_images = refs[:3]

        # enhance_prompt support 
        if self.generation_config.enhance_prompt is not None:
            if not supports_enhance_prompt(self.model):
                logger.warning(
                    "enhance_prompt not supported for model {}; ignoring",
                    self.model.value,
                )
                self.generation_config.enhance_prompt = None

        return self

class GeneratedVideo(BaseModel):
    mime_type: str
    file_name: str
    provider_uri: Optional[str] = None
    s3_url: Optional[str] = None


class VeoVideoResult(BaseModel):
    model: str
    texts: List[str] = Field(default_factory=list)
    videos: List[GeneratedVideo] = Field(default_factory=list)
    s3_urls: List[str] = Field(default_factory=list)
    provider_uris: List[str] = Field(default_factory=list)