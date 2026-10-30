# ====================== utf-8 encoding ==============================
from __future__ import annotations

import asyncio
import shutil
import subprocess
from typing import Callable, ClassVar, List, Optional, Tuple, Union

from pydantic import BaseModel, Field, model_validator, validator


# Utility: lightweight async retry
async def _retry_async(
    fn: Callable[..., asyncio.Future],
    *args,
    retries: int = 3,
    backoff_s: float = 0.5,
    max_backoff: float = 8.0,
    **kwargs,
):
    """Simple exponential backoff retry wrapper for async functions."""
    last_exc = None
    delay = backoff_s
    for attempt in range(1, retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt == retries:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_backoff)
    if last_exc:
        raise last_exc


# Text-to-Speech (TTS) Models
class TTSOutputFormat(BaseModel):
    VALID_CONTAINERS: ClassVar[List[str]] = ["wav", "mp3", "ogg", "flac"]
    VALID_ENCODINGS: ClassVar[List[str]] = ["pcm_s16le", "pcm_f32le", "opus", "mp3", "flac"]

    container: str = Field("wav")
    sample_rate: int = Field(44100, ge=1)
    encoding: str = Field("pcm_s16le")

    @validator("container")
    def _validate_container(cls, v: str) -> str:
        if v not in cls.VALID_CONTAINERS:
            raise ValueError(f"unsupported container: {v}")
        return v

    @validator("encoding")
    def _validate_encoding(cls, v: str) -> str:
        if v not in cls.VALID_ENCODINGS:
            raise ValueError(f"unsupported encoding: {v}")
        return v


class TTSVoice(BaseModel):
    mode: str = Field("id")
    id: Optional[str] = "6ccbfb76-1fc6-48f7-b71d-91ac6298247b"


class TTSOptions(BaseModel):
    """Options for Cartesia TTS (Sonic-3)."""

    model_id: str = Field("sonic-3")
    voice: TTSVoice = Field(default_factory=TTSVoice)
    language: str = Field("en")
    output_format: TTSOutputFormat = Field(default_factory=TTSOutputFormat)
    ssml: bool = Field(False)
    raw_ssml: Optional[str] = None
    speed: Optional[float] = None  # 0.3 - 3.0 typical
    volume: Optional[float] = None  # 0.2 - 5.0 range
    emotion: Optional[str] = None  # e.g. "happy", "sad", "angry"
    chunk_write_size: int = Field(16384, gt=0)

    @validator("speed")
    def _validate_speed(cls, v):
        if v is None:
            return v
        if not (0.3 <= v <= 3.0):
            raise ValueError("speed out of reasonable bounds (0.3 - 3.0)")
        return v

    @validator("volume")
    def _validate_volume(cls, v):
        if v is None:
            return v
        if not (0.2 <= v <= 5.0):
            raise ValueError("volume out of reasonable bounds (0.2 - 5.0)")
        return v

    @model_validator(mode="after")
    def ensure_output_format_valid(self):
        if not isinstance(self.output_format, TTSOutputFormat):
            raise ValueError("output_format must be a valid TTSOutputFormat model")
        return self


# Speech-to-Text (STT) Models
class STTOptions(BaseModel):
    """Options for Cartesia STT (ink-whisper)."""

    model: str = Field("ink-whisper")
    language: str = Field("en")  # ISO-639-1
    timestamps: bool = Field(False)
    encoding: str = Field("pcm_s16le")
    sample_rate: int = Field(16000, gt=0)
    interim_results: bool = Field(True)
    min_volume: float = Field(0.01, ge=0.0)
    max_silence_duration_secs: float = Field(0.6, ge=0.0)
    send_chunk_size: int = Field(4096, gt=0)

    @validator("encoding")
    def _validate_encoding(cls, v: str) -> str:
        if v not in {"pcm_s16le", "pcm_f32le", "opus"}:
            raise ValueError("unsupported stt encoding")
        return v


# SSML Builder Helpers
class SSMLBuilder:
    """Helpers to build SSML-like tags supported by Sonic-3."""

    def __init__(self):
        self.parts: List[str] = []

    def add_text(self, text: str) -> "SSMLBuilder":
        self.parts.append(text)
        return self

    def add_speed(self, ratio: Union[float, str], inner: str) -> "SSMLBuilder":
        self.parts.append(f'<speed ratio="{ratio}">{inner}</speed>')
        return self

    def add_volume(self, ratio: Union[float, str], inner: str) -> "SSMLBuilder":
        self.parts.append(f'<volume ratio="{ratio}">{inner}</volume>')
        return self

    def add_emotion(self, emotion: str, inner: str) -> "SSMLBuilder":
        self.parts.append(f'<emotion value="{emotion}">{inner}</emotion>')
        return self

    def add_pause(self, seconds: float) -> "SSMLBuilder":
        self.parts.append(f'<break time="{seconds}s" />')
        return self

    def add_laughter(self) -> "SSMLBuilder":
        self.parts.append("[laughter]")
        return self

    def render(self) -> str:
        return "".join(self.parts)


# Utility: audio re-encoding
def ensure_audio_pcm(
    src_path: str,
    dst_path: str,
    sample_rate: int = 16000,
    encoding: str = "pcm_s16le",
) -> Tuple[str, int, str]:
    """
    Convert `src_path` -> `dst_path` in-place if needed. Returns (dst_path, sample_rate, encoding).
    Uses pydub (if available), else ffmpeg CLI if present. Raises if neither available.
    """
    try:
        from pydub import AudioSegment  # type: ignore
    except Exception:
        AudioSegment = None

    if encoding == "pcm_s16le":
        target_codec = "pcm_s16le"
    elif encoding == "pcm_f32le":
        target_codec = "pcm_f32le"
    elif encoding == "opus":
        target_codec = "libopus"
    else:
        raise ValueError(f"unsupported target encoding: {encoding}")

    if AudioSegment is not None:
        seg = AudioSegment.from_file(src_path)
        seg = seg.set_frame_rate(sample_rate).set_channels(1)
        seg.export(dst_path, format=dst_path.split(".")[-1] or "wav", codec=target_codec)
        return dst_path, sample_rate, encoding

    # fallback to ffmpeg CLI
    if shutil.which("ffmpeg"):
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            src_path,
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-c:a",
            target_codec,
            dst_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg conversion failed: {proc.stderr}")
        return dst_path, sample_rate, encoding

    raise RuntimeError(
        "audio conversion requires either 'pydub' (and ffmpeg) or ffmpeg CLI available on PATH"
    )
