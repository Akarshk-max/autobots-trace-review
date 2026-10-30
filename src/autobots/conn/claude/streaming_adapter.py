from collections import defaultdict
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field


class ClaudeStreamSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = ""
    thinking: str = ""
    input_json_by_block: dict[int, str] = Field(default_factory=dict)
    signatures_by_block: dict[int, str] = Field(default_factory=dict)
    usage: dict[str, int] = Field(default_factory=dict)
    stop_reason: str | None = None
    raw_events_seen: int = 0


class ClaudeStreamAccumulator:
    def __init__(self) -> None:
        self._text_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self._input_json: dict[int, list[str]] = defaultdict(list)
        self._signatures: dict[int, str] = {}
        self._usage: dict[str, int] = {}
        self._stop_reason: str | None = None
        self._events_seen: int = 0

    def ingest(self, event: Any) -> None:
        self._events_seen += 1

        event_type = getattr(event, "type", None)
        if event_type is None and isinstance(event, dict):
            event_type = event.get("type")

        try:
            if event_type == "content_block_delta":
                delta = getattr(event, "delta", None)
                if delta is None and isinstance(event, dict):
                    delta = event.get("delta", {})
                delta_type = getattr(delta, "type", None) if delta is not None else None
                if delta_type is None and isinstance(delta, dict):
                    delta_type = delta.get("type")

                if delta_type == "text_delta":
                    text = getattr(delta, "text", None)
                    if text is None and isinstance(delta, dict):
                        text = delta.get("text", "")
                    self._text_parts.append(text or "")

                elif delta_type == "thinking_delta":
                    thinking = getattr(delta, "thinking", None)
                    if thinking is None and isinstance(delta, dict):
                        thinking = delta.get("thinking", "")
                    self._thinking_parts.append(thinking or "")

                elif delta_type == "input_json_delta":
                    block_index = getattr(event, "index", 0)
                    if isinstance(event, dict):
                        block_index = event.get("index", 0)
                    partial_json = getattr(delta, "partial_json", None)
                    if partial_json is None and isinstance(delta, dict):
                        partial_json = delta.get("partial_json", "")
                    self._input_json[int(block_index)].append(partial_json or "")

                elif delta_type == "signature_delta":
                    block_index = getattr(event, "index", 0)
                    if isinstance(event, dict):
                        block_index = event.get("index", 0)
                    signature = getattr(delta, "signature", None)
                    if signature is None and isinstance(delta, dict):
                        signature = delta.get("signature", "")
                    self._signatures[int(block_index)] = signature or ""

            elif event_type == "message_delta":
                delta = getattr(event, "delta", None)
                stop_reason = getattr(delta, "stop_reason", None) if delta is not None else None
                if stop_reason is None and isinstance(delta, dict):
                    stop_reason = delta.get("stop_reason")
                if stop_reason:
                    self._stop_reason = stop_reason

                usage = getattr(event, "usage", None)
                if usage is not None:
                    if hasattr(usage, "model_dump"):
                        usage_dict = usage.model_dump()
                    elif isinstance(usage, dict):
                        usage_dict = usage
                    else:
                        usage_dict = {}
                    for key, value in usage_dict.items():
                        if isinstance(value, int):
                            self._usage[key] = value

            elif event_type == "message_stop":
                self._stop_reason = self._stop_reason or "end_turn"

            elif event_type in {
                "message_start",
                "content_block_start",
                "content_block_stop",
                "ping",
                "error",
            }:
                return

            else:
                logger.debug("Unhandled Claude stream event type={}", event_type)
        except Exception as exc:
            logger.exception("Error ingesting Claude stream event: {}", exc)

    def snapshot(self) -> ClaudeStreamSnapshot:
        return ClaudeStreamSnapshot(
            text="".join(self._text_parts),
            thinking="".join(self._thinking_parts),
            input_json_by_block={k: "".join(v) for k, v in self._input_json.items()},
            signatures_by_block=self._signatures,
            usage=self._usage,
            stop_reason=self._stop_reason,
            raw_events_seen=self._events_seen,
        )