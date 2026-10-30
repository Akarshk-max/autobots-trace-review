from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from loguru import logger
from pydantic import BaseModel, Field, field_validator, model_validator


class NanoBananaModel(str, Enum):
    FLASH_IMAGE = "gemini-2.5-flash-image"
    PRO_IMAGE_PREVIEW = "gemini-3-pro-image-preview"
    FLASH_IMAGE_PREVIEW= "gemini-3.1-flash-image-preview"


class ResponseModality(str, Enum):
    TEXT = "TEXT"
    IMAGE = "IMAGE"


class ImageAspectRatio(str, Enum):
    R_1_1 = "1:1"
    R_2_3 = "2:3"
    R_3_2 = "3:2"
    R_3_4 = "3:4"
    R_4_3 = "4:3"
    R_4_5 = "4:5"
    R_5_4 = "5:4"
    R_9_16 = "9:16"
    R_16_9 = "16:9"
    R_21_9 = "21:9"


class ImageSize(str, Enum):
    K1 = "1K"
    K2 = "2K"
    K4 = "4K"


class HarmCategory(str, Enum):
    HATE_SPEECH = "HARM_CATEGORY_HATE_SPEECH"
    HARASSMENT = "HARM_CATEGORY_HARASSMENT"
    SEXUALLY_EXPLICIT = "HARM_CATEGORY_SEXUALLY_EXPLICIT"
    DANGEROUS_CONTENT = "HARM_CATEGORY_DANGEROUS_CONTENT"
    CIVIC_INTEGRITY = "HARM_CATEGORY_CIVIC_INTEGRITY"


class HarmBlockThreshold(str, Enum):
    OFF = "OFF"
    BLOCK_NONE = "BLOCK_NONE"
    BLOCK_ONLY_HIGH = "BLOCK_ONLY_HIGH"
    BLOCK_MEDIUM_AND_ABOVE = "BLOCK_MEDIUM_AND_ABOVE"
    BLOCK_LOW_AND_ABOVE = "BLOCK_LOW_AND_ABOVE"
    UNSPECIFIED = "HARM_BLOCK_THRESHOLD_UNSPECIFIED"


class NanoBananaErrorPayload(BaseModel):
    code: str = Field(..., description="Stable internal error code")
    message: str
    retryable: bool = False
    provider: Literal["gemini"] = "gemini"
    http_status: Optional[int] = None
    details: Optional[Dict[str, Any]] = None


class NanoBananaException(Exception):
    def __init__(self, payload: NanoBananaErrorPayload):
        super().__init__(payload.message)
        self.payload = payload


def _is_retryable_exception(e: Exception) -> bool:
    msg = str(e).lower()
    # Transport-ish
    if "timeout" in msg or "timed out" in msg:
        return True
    if "connection" in msg and ("reset" in msg or "refused" in msg):
        return True
    # Rate limits / transient
    if "rate limit" in msg or "429" in msg:
        return True
    if "temporarily" in msg or "unavailable" in msg or "deadline" in msg:
        return True
    return False


class NanoBananaSafetySetting(BaseModel):
    category: HarmCategory
    threshold: HarmBlockThreshold


class NanoBananaImageConfig(BaseModel):
    aspect_ratio: Optional[ImageAspectRatio] = None
    image_size: Optional[ImageSize] = None

    @model_validator(mode="after")
    def _validate(self):
        return self


class NanoBananaGenerateConfig(BaseModel):
    response_modalities: List[ResponseModality] = Field(
        default_factory=lambda: [ResponseModality.IMAGE],
        description="Use [IMAGE] for image-only, or [TEXT, IMAGE] for both.",
    )
    image_config: Optional[NanoBananaImageConfig] = None
    safety_settings: Optional[List[NanoBananaSafetySetting]] = None

    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    candidate_count: Optional[int] = None
    seed: Optional[int] = None
    max_output_tokens: Optional[int] = None
    stop_sequences: Optional[List[str]] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None

    tools: Optional[List[str]] = None

    @field_validator("response_modalities", mode="before")
    @classmethod
    def _normalize_modalities(cls, v):
        if v is None:
            return v
        out: List[ResponseModality] = []
        for item in v:
            s = str(item).strip().upper()
            if s == "IMAGE":
                out.append(ResponseModality.IMAGE)
            elif s == "TEXT":
                out.append(ResponseModality.TEXT)
            else:
                raise ValueError(f"Invalid response modality: {item}")
        return out


class NanoBananaImageInput(BaseModel):
    url: Optional[str] = None
    file_path: Optional[str] = None
    base64_content: Optional[str] = None

    file_name: Optional[str] = None
    mime_type: Optional[str] = None

    @model_validator(mode="after")
    def validate_one_source(self):
        sources = [bool(self.url), bool(self.file_path), bool(self.base64_content)]
        if sum(sources) != 1:
            raise ValueError("Provide exactly one of: url, file_path, base64_content")
        return self


class NanoBananaImageRequest(BaseModel):
    model: NanoBananaModel = NanoBananaModel.FLASH_IMAGE
    prompt: str = Field(..., min_length=1)
    images: Optional[List[NanoBananaImageInput]] = None
    generation_config: NanoBananaGenerateConfig = Field(default_factory=NanoBananaGenerateConfig)

    # Default to S3 URLs for generated images
    upload_to_s3: bool = True
    s3_prefix: str = "nanobanana-images"

    # If S3 fails, should we fail the request or still return base64 images? - I added this for having alternate option for now
    fail_on_s3_error: bool = True

    @model_validator(mode="after")
    def validate_model_specifics(self):
        if self.model == NanoBananaModel.FLASH_IMAGE and self.generation_config.image_config:
            self.generation_config.image_config.image_size = None

        if self.generation_config.image_config and self.generation_config.image_config.image_size:
            if self.model != NanoBananaModel.PRO_IMAGE_PREVIEW:
                raise ValueError(
                    "image_config.image_size is only supported for gemini-3-pro-image-preview"
                )

        if self.images:
            n = len(self.images)
            if self.model == NanoBananaModel.FLASH_IMAGE and n > 3:
                logger.warning("Flash Image works best with up to 3 input images; got {}", n)
            if self.model == NanoBananaModel.PRO_IMAGE_PREVIEW and n > 14:
                logger.warning("Pro Image supports up to 14 total input images; got {}", n)

        return self


class GeneratedImage(BaseModel):
    mime_type: str
    bytes_b64: str
    file_name: str


class NanoBananaImageResult(BaseModel):
    model: str
    texts: List[str] = Field(default_factory=list)
    images: List[GeneratedImage] = Field(default_factory=list)
    s3_urls: List[str] = Field(default_factory=list)
