import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.autobots.conn.gemini.message_normalizer_gemini import GeminiMessageNormalizer
from src.autobots.conn.gemini.thought_signatures_gemini import GeminiThoughtSignatureHelper


class GeminiStreamSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = ""
    assistant_message: dict[str, Any] | None = None
    function_calls: list[dict[str, Any]] = Field(default_factory=list)
    finish_reason: str | None = None
    usage_metadata: dict[str, Any] = Field(default_factory=dict)
    thought_signatures: list[str] = Field(default_factory=list)
    raw_chunks_seen: int = 0


class GeminiStreamAccumulator:
    def __init__(self) -> None:
        self._text_chunks: list[str] = []
        self._assistant_role: str = "model"
        self._function_call_parts_by_id: dict[str, dict[str, Any]] = {}
        self._function_call_order: list[str] = []
        self._other_parts: list[dict[str, Any]] = []
        self._other_part_identities: set[str] = set()
        self._finish_reason: str | None = None
        self._usage_metadata: dict[str, Any] = {}
        self._thought_signatures: list[str] = []
        self._raw_chunks_seen: int = 0

    @staticmethod
    def _tool_call_id(call: dict[str, Any], *, fallback_index: int) -> str:
        return str(call.get("id") or call.get("name") or fallback_index)

    @staticmethod
    def _part_identity(part: dict[str, Any]) -> str:
        try:
            return json.dumps(part, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            return repr(part)

    def _append_other_part(self, part: dict[str, Any]) -> None:
        identity = self._part_identity(part)
        if identity in self._other_part_identities:
            return
        self._other_part_identities.add(identity)
        self._other_parts.append(part)

    def ingest(self, chunk: Any) -> None:
        self._raw_chunks_seen += 1

        plain = GeminiMessageNormalizer.normalize_response(chunk)

        chunk_text = getattr(chunk, "text", None)
        if isinstance(chunk_text, str) and chunk_text:
            self._text_chunks.append(chunk_text)

        candidate_content = GeminiMessageNormalizer.first_candidate_content(plain)
        if candidate_content is not None:
            self._assistant_role = str(candidate_content.get("role") or self._assistant_role or "model")
            parts = candidate_content.get("parts") or []
            if isinstance(parts, list):
                self._thought_signatures.extend(
                    GeminiThoughtSignatureHelper.extract_signatures_from_parts(parts)
                )
                for raw_part in parts:
                    if not isinstance(raw_part, dict):
                        continue

                    if raw_part.get("role") is not None:
                        if set(raw_part.keys()) == {"role"}:
                            continue
                        raw_part = dict(raw_part)
                        raw_part.pop("role", None)
                        if not raw_part:
                            continue

                    function_call = raw_part.get("function_call") or raw_part.get("functionCall")
                    if isinstance(function_call, dict):
                        tool_id = self._tool_call_id(
                            function_call,
                            fallback_index=len(self._function_call_order),
                        )
                        if tool_id not in self._function_call_parts_by_id:
                            self._function_call_order.append(tool_id)
                        self._function_call_parts_by_id[tool_id] = raw_part
                        continue

                    text = raw_part.get("text")
                    if isinstance(text, str) and text:
                        continue

                    self._append_other_part(raw_part)

        usage_metadata = plain.get("usage_metadata") or plain.get("usageMetadata")
        if isinstance(usage_metadata, dict):
            self._usage_metadata = usage_metadata

        finish_reason = GeminiMessageNormalizer.first_finish_reason(plain)
        if finish_reason:
            self._finish_reason = finish_reason

    def snapshot(self) -> GeminiStreamSnapshot:
        effective_text = "".join(self._text_chunks)

        assistant_parts: list[dict[str, Any]] = []
        if effective_text:
            assistant_parts.append({"text": effective_text})
        for tool_id in self._function_call_order:
            part = self._function_call_parts_by_id.get(tool_id)
            if isinstance(part, dict):
                assistant_parts.append(part)
        assistant_parts.extend(self._other_parts)

        assistant_message = None
        if assistant_parts:
            assistant_message = {
                "role": self._assistant_role or "model",
                "parts": assistant_parts,
            }

        deduped_signatures: list[str] = []
        seen: set[str] = set()
        for signature in self._thought_signatures:
            if not signature or signature in seen:
                continue
            seen.add(signature)
            deduped_signatures.append(signature)

        function_calls: list[dict[str, Any]] = []
        for tool_id in self._function_call_order:
            part = self._function_call_parts_by_id.get(tool_id)
            if not isinstance(part, dict):
                continue
            function_call = part.get("function_call") or part.get("functionCall")
            if isinstance(function_call, dict):
                function_calls.append(function_call)

        return GeminiStreamSnapshot(
            text=effective_text,
            assistant_message=assistant_message,
            function_calls=function_calls,
            finish_reason=self._finish_reason,
            usage_metadata=self._usage_metadata,
            thought_signatures=deduped_signatures,
            raw_chunks_seen=self._raw_chunks_seen,
        )
