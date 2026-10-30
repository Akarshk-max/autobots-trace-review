import base64
from typing import Any, Iterable

from loguru import logger

from src.autobots.conn.gemini.model_registry_gemini import (
    GeminiModelRegistry,
    GeminiProvider,
    GeminiThoughtSignatureMode,
)


class GeminiThoughtSignatureHelper:
    @staticmethod
    def encode_signature(value: bytes | str | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return base64.b64encode(value).decode("ascii")

    @staticmethod
    def decode_signature(value: str | bytes | None) -> bytes | str | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            return value

        try:
            return base64.b64decode(value.encode("ascii"), validate=True)
        except Exception:
            return value

    @classmethod
    def normalize_signature_fields(cls, payload: Any) -> Any:
        if isinstance(payload, list):
            return [cls.normalize_signature_fields(item) for item in payload]

        if not isinstance(payload, dict):
            return payload

        normalized: dict[str, Any] = {}
        for key, value in payload.items():
            if key in {"thought_signature", "thoughtSignature"}:
                normalized[key] = cls.encode_signature(value)
                continue
            normalized[key] = cls.normalize_signature_fields(value)
        return normalized

    @classmethod
    def extract_signatures_from_parts(cls, parts: Iterable[dict[str, Any] | Any]) -> list[str]:
        signatures: list[str] = []
        for raw_part in parts:
            if not isinstance(raw_part, dict):
                continue
            for key in ("thought_signature", "thoughtSignature"):
                signature = raw_part.get(key)
                encoded = cls.encode_signature(signature)
                if encoded:
                    signatures.append(encoded)
        return signatures

    @classmethod
    def count_signatures_in_messages(cls, messages: Iterable[dict[str, Any] | Any]) -> int:
        total = 0
        for raw_message in messages:
            if not isinstance(raw_message, dict):
                continue
            parts = raw_message.get("parts") or raw_message.get("content", {}).get("parts") or []
            if isinstance(parts, list):
                total += len(cls.extract_signatures_from_parts(parts))
        return total

    @classmethod
    def signature_mode_for_model(
        cls,
        model: str | None,
        provider: GeminiProvider = GeminiProvider.GEMINI_API,
    ) -> GeminiThoughtSignatureMode:
        return GeminiModelRegistry.resolve(model=model, provider=provider).capabilities.thought_signature_mode

    @classmethod
    def requires_function_call_signature_return(
        cls,
        model: str | None,
        provider: GeminiProvider = GeminiProvider.GEMINI_API,
    ) -> bool:
        return (
            cls.signature_mode_for_model(model=model, provider=provider)
            == GeminiThoughtSignatureMode.REQUIRED_FOR_FUNCTION_CALLS
        )

    @classmethod
    def validate_signature_count_preserved(
        cls,
        *,
        before_messages: Iterable[dict[str, Any] | Any],
        after_messages: Iterable[dict[str, Any] | Any],
        model: str | None,
        provider: GeminiProvider = GeminiProvider.GEMINI_API,
    ) -> None:
        before_count = cls.count_signatures_in_messages(before_messages)
        after_count = cls.count_signatures_in_messages(after_messages)

        if after_count < before_count:
            logger.warning(
                "Gemini thought signatures appear to have been dropped model={} provider={} before={} after={}",
                model,
                provider.value,
                before_count,
                after_count,
            )

            if cls.requires_function_call_signature_return(model=model, provider=provider):
                raise ValueError(
                    "Gemini function-call thought signatures were lost during message normalization"
                )