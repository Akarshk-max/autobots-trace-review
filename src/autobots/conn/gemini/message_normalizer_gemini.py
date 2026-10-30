from __future__ import annotations

import base64
from datetime import date, datetime
from enum import Enum
from typing import Any

from src.autobots.conn.gemini.thought_signatures_gemini import GeminiThoughtSignatureHelper


class GeminiMessageNormalizer:
    @classmethod
    def to_plain(cls, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value

        if isinstance(value, bytes):
            return base64.b64encode(value).decode("ascii")

        if isinstance(value, Enum):
            return value.value

        if isinstance(value, (datetime, date)):
            return value.isoformat()

        if isinstance(value, list):
            return [cls.to_plain(item) for item in value]

        if isinstance(value, tuple):
            return [cls.to_plain(item) for item in value]

        if isinstance(value, dict):
            normalized = {str(key): cls.to_plain(item) for key, item in value.items()}
            return GeminiThoughtSignatureHelper.normalize_signature_fields(normalized)

        if hasattr(value, "model_dump"):
            dumped = value.model_dump(exclude_none=True)
            return cls.to_plain(dumped)

        if hasattr(value, "dict") and callable(value.dict):
            dumped = value.dict(exclude_none=True)
            return cls.to_plain(dumped)

        return str(value)

    @staticmethod
    def _normalize_role(role: Any) -> str:
        role_str = str(role or "user").lower()
        if role_str == "assistant":
            return "model"
        if role_str not in {"user", "model"}:
            return "user"
        return role_str

    @classmethod
    def _part_from_legacy_message_dict(cls, plain: dict[str, Any]) -> dict[str, Any]:
        content = plain.get("content")
        if isinstance(content, str):
            return {"text": content}

        if isinstance(content, list):
            converted = cls.normalize_parts(content)
            if len(converted) == 1:
                return converted[0]
            return {"text": cls.extract_text_from_parts(converted)}

        text = plain.get("text")
        if isinstance(text, str):
            return {"text": text}

        return {"text": str(content or "")}

    @classmethod
    def normalize_part(cls, part: Any) -> dict[str, Any]:
        plain = cls.to_plain(part)

        if isinstance(plain, dict):
            # Legacy/OpenAI-style message accidentally nested inside Gemini parts.
            if "role" in plain and ("content" in plain or "parts" in plain):
                if "parts" in plain and isinstance(plain.get("parts"), list):
                    normalized_nested_parts = cls.normalize_parts(plain.get("parts"))
                    if len(normalized_nested_parts) == 1:
                        return normalized_nested_parts[0]
                    return {"text": cls.extract_text_from_parts(normalized_nested_parts)}
                return cls._part_from_legacy_message_dict(plain)

            # OpenAI content block format.
            if plain.get("type") == "text" and isinstance(plain.get("text"), str):
                normalized = {"text": plain.get("text", "")}
                for key in ("thought_signature", "thoughtSignature"):
                    if key in plain:
                        normalized[key] = plain[key]
                return normalized

            # Gemini-safe parts should pass through unchanged.
            return plain

        if isinstance(plain, str):
            return {"text": plain}

        return {"text": str(plain)}

    @classmethod
    def normalize_parts(cls, parts: Any) -> list[dict[str, Any]]:
        plain = cls.to_plain(parts)
        if plain is None:
            return []

        if isinstance(plain, list):
            normalized_parts: list[dict[str, Any]] = []
            for item in plain:
                normalized = cls.normalize_part(item)
                if not isinstance(normalized, dict):
                    continue

                if "role" in normalized:
                    normalized = dict(normalized)
                    normalized.pop("role", None)
                    if not normalized:
                        continue

                try:
                    from google.genai import (
                        types,  # local import to avoid import costs unless needed
                    )
                    allowed = set(types.Part.model_fields.keys()) | {
                        field.alias for field in types.Part.model_fields.values() if field.alias
                    }
                except Exception:
                    allowed = {
                        "text",
                        "inline_data",
                        "inlineData",
                        "file_data",
                        "fileData",
                        "function_call",
                        "functionCall",
                        "function_response",
                        "functionResponse",
                        "executable_code",
                        "executableCode",
                        "code_execution_result",
                        "codeExecutionResult",
                        "video_metadata",
                        "videoMetadata",
                        "tool_call",
                        "toolCall",
                        "tool_response",
                        "toolResponse",
                        "part_metadata",
                        "partMetadata",
                        "media_resolution",
                        "mediaResolution",
                        "thought",
                        "thought_signature",
                        "thoughtSignature",
                    }
                normalized = {k: v for k, v in normalized.items() if k in allowed}
                if not normalized:
                    continue

                normalized_parts.append(normalized)
            return normalized_parts

        return [cls.normalize_part(plain)]

    @classmethod
    def normalize_message(cls, message: Any) -> dict[str, Any]:
        plain = cls.to_plain(message)

        if isinstance(plain, dict):
            role = cls._normalize_role(plain.get("role", "user"))

            # Native Gemini-style message.
            if "parts" in plain and isinstance(plain.get("parts"), list):
                return {
                    "role": role,
                    "parts": cls.normalize_parts(plain.get("parts")),
                }

            # OpenAI-style message with string content.
            if "content" in plain and isinstance(plain.get("content"), str):
                return {
                    "role": role,
                    "parts": [{"text": plain.get("content", "")}],
                }

            # OpenAI-style message with list content blocks.
            if "content" in plain and isinstance(plain.get("content"), list):
                return {
                    "role": role,
                    "parts": cls.normalize_parts(plain.get("content")),
                }

            # Response wrapper content object.
            if "content" in plain and isinstance(plain.get("content"), dict):
                content = plain.get("content") or {}
                return {
                    "role": cls._normalize_role(plain.get("role", content.get("role", "user"))),
                    "parts": cls.normalize_parts(content.get("parts") or content.get("content")),
                }

            # Fallback for dicts with text but no explicit parts/content.
            if isinstance(plain.get("text"), str):
                return {
                    "role": role,
                    "parts": [{"text": plain.get("text", "")}],
                }

        return {"role": "user", "parts": cls.normalize_parts(plain)}

    @classmethod
    def normalize_messages(cls, messages: Any) -> list[dict[str, Any]]:
        plain = cls.to_plain(messages)
        if plain is None:
            return []
        if isinstance(plain, list):
            return [cls.normalize_message(message) for message in plain]
        return [cls.normalize_message(plain)]

    @classmethod
    def extract_text_from_parts(cls, parts: list[dict[str, Any]] | Any) -> str:
        normalized_parts = cls.normalize_parts(parts)
        chunks: list[str] = []
        for part in normalized_parts:
            text = part.get("text")
            if isinstance(text, str) and text:
                chunks.append(text)
        return "\n".join(chunks).strip()

    @classmethod
    def extract_function_calls(cls, parts: list[dict[str, Any]] | Any) -> list[dict[str, Any]]:
        normalized_parts = cls.normalize_parts(parts)
        calls: list[dict[str, Any]] = []
        for part in normalized_parts:
            function_call = part.get("function_call") or part.get("functionCall")
            if isinstance(function_call, dict):
                calls.append(function_call)
        return calls

    @classmethod
    def normalize_response(cls, response: Any) -> dict[str, Any]:
        plain = cls.to_plain(response)
        if not isinstance(plain, dict):
            return {"value": plain}
        return plain

    @classmethod
    def first_candidate_content(cls, response: Any) -> dict[str, Any] | None:
        plain = cls.normalize_response(response)
        candidates = plain.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return None

        first_candidate = candidates[0]
        if not isinstance(first_candidate, dict):
            return None

        content = first_candidate.get("content")
        if not isinstance(content, dict):
            return None

        return cls.normalize_message(content)

    @classmethod
    def first_finish_reason(cls, response: Any) -> str | None:
        plain = cls.normalize_response(response)
        candidates = plain.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return None

        first_candidate = candidates[0]
        if not isinstance(first_candidate, dict):
            return None

        finish_reason = first_candidate.get("finish_reason") or first_candidate.get(
            "finishReason"
        )
        return str(finish_reason) if finish_reason else None
