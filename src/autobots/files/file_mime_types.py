import mimetypes
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse

_GENERIC_BINARY_MIME_TYPES = {
    "application/octet-stream",
    "binary/octet-stream",
}

# Prefer stable explicit mappings for Gemini-supported media families because
# Python's mimetypes table varies across platforms.
_EXTENSION_MIME_OVERRIDES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".text": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/json",
    ".xml": "application/xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".rtf": "application/rtf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".mpga": "audio/mpeg",
    ".aac": "audio/aac",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".flv": "video/x-flv",
    ".webm": "video/webm",
    ".wmv": "video/x-ms-wmv",
    ".3gp": "video/3gpp",
}

_MIME_EXTENSION_OVERRIDES = {
    mime_type: extension for extension, mime_type in _EXTENSION_MIME_OVERRIDES.items()
}


def normalize_mime_type(value: str | None) -> str | None:
    if value is None:
        return None
    mime_type = str(value).strip().lower()
    if not mime_type:
        return None
    if ";" in mime_type:
        mime_type = mime_type.split(";", 1)[0].strip()
    return mime_type or None


def is_generic_binary_mime_type(value: str | None) -> bool:
    return normalize_mime_type(value) in _GENERIC_BINARY_MIME_TYPES


def _candidate_to_path(candidate: str | None) -> str:
    if not candidate:
        return ""
    raw = str(candidate).strip()
    if not raw:
        return ""
    if "://" in raw:
        parsed = urlparse(raw)
        return unquote(parsed.path or "")
    return unquote(raw)


def _guess_from_candidate(candidate: str | None) -> str | None:
    path_like = _candidate_to_path(candidate)
    if not path_like:
        return None

    guessed, _encoding = mimetypes.guess_type(path_like, strict=False)
    normalized = normalize_mime_type(guessed)
    if normalized and not is_generic_binary_mime_type(normalized):
        return normalized

    suffix = Path(path_like).suffix.lower()
    if suffix:
        override = _EXTENSION_MIME_OVERRIDES.get(suffix)
        if override:
            return override
    return None


def guess_mime_type(*candidates: str | None) -> str | None:
    for candidate in candidates:
        resolved = _guess_from_candidate(candidate)
        if resolved:
            return resolved
    return None


def guess_extension_from_mime_type(mime_type: str | None) -> str:
    normalized = normalize_mime_type(mime_type)
    if not normalized:
        return ""
    extension = _MIME_EXTENSION_OVERRIDES.get(normalized)
    if extension:
        return extension
    return mimetypes.guess_extension(normalized, strict=False) or ""


def resolve_file_mime_type(
    *,
    direct_candidates: Iterable[str | None] = (),
    file_name: str | None = None,
    path: str | None = None,
    source_url: str | None = None,
) -> str | None:
    direct_values = [normalize_mime_type(candidate) for candidate in direct_candidates]
    for direct in direct_values:
        if direct and not is_generic_binary_mime_type(direct):
            return direct

    guessed = guess_mime_type(file_name, path, source_url)
    if guessed:
        return guessed

    for direct in direct_values:
        if direct:
            return direct
    return None
