from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote, urlparse

TABULAR_EXTENSIONS = {".csv", ".tsv", ".xls", ".xlsx"}
JSONL_EXTENSIONS = {".jsonl", ".ndjson"}
FILE_SEARCH_MAX_DOC_BYTES = 100 * 1024 * 1024
MEDIA_MIME_PREFIXES = ("image/", "audio/", "video/")
SUPPORTED_APPLICATION_MIME_TYPES = {
    "application/dart",
    "application/ecmascript",
    "application/json",
    "application/ms-java",
    "application/msword",
    "application/pdf",
    "application/sql",
    "application/typescript",
    "application/vnd.curl",
    "application/vnd.dart",
    "application/vnd.ibm.secure-container",
    "application/vnd.jupyter",
    "application/vnd.ms-excel",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    "application/x-csh",
    "application/x-hwp",
    "application/x-hwp-v5",
    "application/x-latex",
    "application/x-php",
    "application/x-powershell",
    "application/x-sh",
    "application/x-shellscript",
    "application/x-tex",
    "application/x-zsh",
    "application/xml",
    "application/zip",
}

@dataclass(frozen=True)
class AutoIngestDecision:
    action: Literal["upload_file", "skip_tabular", "skip_unsupported"]
    mime_type: str | None = None
    reason: str | None = None

def display_name_from_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return "(empty-url)"
    try:
        parsed = urlparse(value)
        path = unquote(parsed.path or "")
        name = path.split("/")[-1] if path else ""
        return name or value
    except Exception:
        return value

def extension_from_url(url: str) -> str:
    name = display_name_from_url(url).lower()
    dot = name.rfind(".")
    if dot < 0:
        return ""
    return name[dot:]

def normalize_mime_type(value: str | None) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    # Strip common parameters like charset.
    if ";" in raw:
        raw = raw.split(";", 1)[0].strip()
    return raw or None

def is_supported_file_search_mime_type(mime_type: str | None) -> bool:
    mt = normalize_mime_type(mime_type)
    if not mt:
        return False
    if mt.startswith("text/"):
        return True
    return mt in SUPPORTED_APPLICATION_MIME_TYPES

def is_text_like_extension(ext: str) -> bool:
    ext = (ext or "").lower().strip()
    if not ext:
        return False
    return ext in {
        ".txt",
        ".md",
        ".markdown",
        ".log",
        ".json",
        ".jsonl",
        ".ndjson",
        ".yaml",
        ".yml",
        ".xml",
        ".html",
        ".htm",
        ".css",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".py",
        ".java",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".rs",
        ".go",
        ".rb",
        ".php",
        ".sql",
        ".sh",
        ".zsh",
        ".ps1",
    }


def decide_auto_ingest(*, url: str, mime_type: str | None) -> AutoIngestDecision:
    normalized_url = str(url or "").strip()
    ext = extension_from_url(normalized_url)
    mt = normalize_mime_type(mime_type)

    if ext in TABULAR_EXTENSIONS:
        return AutoIngestDecision(action="skip_tabular", reason=f"tabular-extension:{ext}")

    if mt in {
        "text/csv",
        "text/tab-separated-values",
        "text/tsv",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }:
        return AutoIngestDecision(action="skip_tabular", reason=f"tabular-mime:{mt}")

    if mt and any(mt.startswith(prefix) for prefix in MEDIA_MIME_PREFIXES):
        return AutoIngestDecision(action="skip_unsupported", reason=f"media-mime:{mt}")

    if ext in JSONL_EXTENSIONS:
        return AutoIngestDecision(action="upload_file", mime_type="text/plain", reason=f"jsonl-as-text:{ext}")

    if is_supported_file_search_mime_type(mt):
        return AutoIngestDecision(action="upload_file", mime_type=mt, reason="supported-mime")

    if is_text_like_extension(ext):
        return AutoIngestDecision(action="upload_file", mime_type="text/plain", reason="text-fallback-from-ext")

    return AutoIngestDecision(action="upload_file", mime_type=None, reason="needs-sniff")


def extract_text_fallback(
    data: bytes,
    *,
    max_chars: int = 200_000,
) -> str | None:
    if not data:
        return ""

    if b"\x00" in data:
        return None

    text: str
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")

    if not _looks_like_text(text):
        try:
            text2 = data.decode("latin-1")
        except Exception:
            return None
        if not _looks_like_text(text2):
            return None
        text = text2

    if len(text) > max_chars:
        text = text[:max_chars]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _looks_like_text(text: str) -> bool:
    if not text:
        return True

    replacement_ratio = text.count("\ufffd") / max(1, len(text))
    if replacement_ratio > 0.02:
        return False

    control = 0
    for ch in text:
        o = ord(ch)
        if o < 32 and ch not in {"\n", "\r", "\t"}:
            control += 1
    control_ratio = control / max(1, len(text))
    return control_ratio < 0.01


def build_manifest_entry(
    *,
    url: str,
    display_name: str | None,
    status: str,
    current_turn: int,
    metadata: dict[str, Any] | None = None,
    document_name: str | None = None,
    mime_type: str | None = None,
) -> dict[str, Any]:
    return {
        "document_name": document_name,
        "display_name": display_name,
        "source_url": url,
        "status": status,
        "metadata": metadata or {},
        "mime_type": mime_type,
        "last_seen_turn": current_turn,
    }