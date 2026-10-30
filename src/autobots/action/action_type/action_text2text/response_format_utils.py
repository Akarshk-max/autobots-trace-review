from __future__ import annotations

import json
import mimetypes
from typing import Any

NDJSON_CONTENT_TYPES = {
    "application/x-ndjson",
    "application/ndjson",
    "application/jsonl",
    "application/x-jsonlines",
    "application/json-seq",
}

BASE_TEXT_CONTENT_TYPES = {
    "application/json",
    "application/x-www-form-urlencoded",
    "application/xml",
    "text/xml",
}


def normalize_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower() or None


def is_ndjson_content_type(content_type: str | None) -> bool:
    ct = normalize_content_type(content_type)
    return bool(ct and ct in NDJSON_CONTENT_TYPES)


def is_text_content_type(content_type: str | None) -> bool:
    ct = normalize_content_type(content_type)
    if not ct:
        return False
    return (
        ct.startswith("text/")
        or ct in BASE_TEXT_CONTENT_TYPES
        or ct.endswith("+json")
        or ct.endswith("+xml")
        or is_ndjson_content_type(ct)
    )


def _normalize_extension(ext: str) -> str:
    if ext == ".jpe":
        return ".jpg"
    return ext


def infer_text_artifact_extension(content_type: str | None, text: str) -> str:
    ct = normalize_content_type(content_type)
    if is_ndjson_content_type(ct):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) == 1:
            try:
                parsed = json.loads(lines[0])
            except Exception:
                return ".ndjson"
            if isinstance(parsed, (dict, list)):
                return ".json"
            return ".ndjson"
        return ".ndjson"
    guessed = mimetypes.guess_extension(ct or "") or ".txt"
    return _normalize_extension(guessed)


def _truncate(value: Any, limit: int = 120) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def build_text_artifact_summary(content_type: str | None, text: str) -> str:
    ct = normalize_content_type(content_type)
    if is_ndjson_content_type(ct):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return "Saved empty NDJSON/text artifact (0 records)."
        if len(lines) == 1:
            try:
                parsed = json.loads(lines[0])
            except Exception:
                return f"Saved single-line NDJSON/text artifact (1 line). Sample: {_truncate(lines[0])}"
            if isinstance(parsed, dict):
                keys = list(parsed.keys())[:8]
                return f"Saved structured text artifact (1 JSON object). Keys: {', '.join(keys)}"
            if isinstance(parsed, list):
                return f"Saved structured text artifact (1 JSON array line, {len(parsed)} items)."
            return f"Saved structured text artifact (1 scalar JSON line). Sample: {_truncate(parsed)}"
        sample = []
        sample_keys: list[str] = []
        for line in lines[:3]:
            try:
                parsed = json.loads(line)
            except Exception:
                sample.append(_truncate(line))
                continue
            if isinstance(parsed, dict):
                if not sample_keys:
                    sample_keys = list(parsed.keys())[:8]
                compact = {k: parsed[k] for k in list(parsed.keys())[:3]}
                sample.append(_truncate(compact))
            else:
                sample.append(_truncate(parsed))
        bits = [f"Saved NDJSON artifact ({len(lines)} records)."]
        if sample_keys:
            bits.append(f"Sample keys: {', '.join(sample_keys)}.")
        if sample:
            bits.append(f"Sample records: {' | '.join(sample)}")
        return ' '.join(bits)
    lines = [line for line in text.splitlines() if line.strip()]
    return f"Saved text artifact ({len(lines)} non-empty lines)."
