from dataclasses import dataclass, field
from typing import Any

from openai.types.chat import ChatCompletion, ChatCompletionChunk

from src.autobots.conn.openai.openai_responses.tool_args_streamer import ToolArgsStreamer
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify


@dataclass
class StreamingResponseBuilder:
    ctx: Context
    notify_main: Notify | None = None
    notify_complimentary: Notify | None = None

    id_completion: str = ""
    created: int = 0
    model: str = ""
    usage: Any = None
    finish_reason: str | None = None

    message: dict[str, Any] | None = None
    content_main: str = ""
    content_complimentary: str = ""

    tool_calls: dict[int, dict[str, Any]] = field(default_factory=dict)
    tool_args_streamers: dict[int, ToolArgsStreamer] = field(default_factory=dict)

    function_call_name: str = ""
    function_call_arguments: str = ""
    function_call_streamer: ToolArgsStreamer | None = None

    def ingest(self, chunk: ChatCompletionChunk) -> None:
        if not isinstance(chunk, ChatCompletionChunk):
            return

        self.id_completion = chunk.id
        self.created = chunk.created
        self.model = chunk.model
        if chunk.usage:
            self.usage = chunk.usage

        for choice in chunk.choices:
            self._ingest_choice(choice)

    def _ingest_choice(self, choice: Any) -> None:
        delta = choice.delta

        if delta.role:
            if self.message is None:
                self.message = {"role": delta.role, "content": ""}
            else:
                self.message["role"] = delta.role

        if delta.refusal:
            if self.message is None:
                self.message = {"role": "assistant", "content": ""}
            self.message["refusal"] = (self.message.get("refusal") or "") + delta.refusal
            self.content_complimentary += delta.refusal
            if self.notify_complimentary:
                self.notify_complimentary(self.ctx, self.content_complimentary)

        if delta.content:
            if self.message is None:
                self.message = {"role": "assistant", "content": ""}
            self.content_main += delta.content
            if self.notify_main:
                self.notify_main(self.ctx, self.content_main)
            self.message["content"] = (self.message.get("content") or "") + delta.content

        if delta.tool_calls:
            for tool_call in delta.tool_calls:
                self._ingest_tool_call(tool_call)

        if delta.function_call:
            self._ingest_function_call(delta.function_call)

        if choice.finish_reason:
            self.finish_reason = choice.finish_reason
            self.content_complimentary = f"Finish reason: {choice.finish_reason}"
            if self.notify_complimentary:
                self.notify_complimentary(self.ctx, self.content_complimentary)

    def _ingest_tool_call(self, tool_call: Any) -> None:
        idx = getattr(tool_call, "index", None)
        if idx is None:
            idx = max(self.tool_calls.keys(), default=-1) + 1

        entry = self.tool_calls.get(idx)
        if entry is None:
            entry = {"id": "", "name": "", "arguments": ""}
            self.tool_calls[idx] = entry

        if getattr(tool_call, "id", None):
            entry["id"] = tool_call.id or entry.get("id") or ""

        fn = getattr(tool_call, "function", None)
        if fn is not None and getattr(fn, "name", None):
            entry["name"] = fn.name or entry.get("name") or ""

        args_delta = ""
        if fn is not None and getattr(fn, "arguments", None):
            args_delta = fn.arguments or ""

        tool_name = entry.get("name") or "tool"
        streamer = self.tool_args_streamers.get(idx)
        if streamer is None or (streamer.tool_name == "tool" and tool_name != "tool"):
            streamer = ToolArgsStreamer(tool_name, key_char_limit=100, value_char_limit=100)
            existing_args = entry.get("arguments") or ""
            if existing_args:
                streamer.ingest(existing_args)
            self.tool_args_streamers[idx] = streamer
            self.content_complimentary = streamer.render()
            if self.notify_complimentary:
                self.notify_complimentary(self.ctx, self.content_complimentary)
            streamer.mark_clean()

        if args_delta:
            entry["arguments"] = (entry.get("arguments") or "") + args_delta
            streamer.ingest(args_delta)
            if streamer.has_updates():
                self.content_complimentary = streamer.render()
                if self.notify_complimentary:
                    self.notify_complimentary(self.ctx, self.content_complimentary)
                streamer.mark_clean()

    def _ingest_function_call(self, fc: Any) -> None:
        if self.message is None:
            self.message = {"role": "assistant", "content": ""}

        fc_name = getattr(fc, "name", None)
        if fc_name:
            self.function_call_name = str(fc_name)

        args_delta = getattr(fc, "arguments", None) or ""
        if args_delta:
            self.function_call_arguments += str(args_delta)

        preview_name = self.function_call_name or "function"
        if self.function_call_streamer is None or self.function_call_streamer.tool_name != preview_name:
            self.function_call_streamer = ToolArgsStreamer(preview_name, key_char_limit=100, value_char_limit=100)
            if self.function_call_arguments:
                self.function_call_streamer.ingest(self.function_call_arguments)
            self.content_complimentary = self.function_call_streamer.render()
            if self.notify_complimentary:
                self.notify_complimentary(self.ctx, self.content_complimentary)
            self.function_call_streamer.mark_clean()
        elif args_delta:
            self.function_call_streamer.ingest(str(args_delta))
            if self.function_call_streamer.has_updates():
                self.content_complimentary = self.function_call_streamer.render()
                if self.notify_complimentary:
                    self.notify_complimentary(self.ctx, self.content_complimentary)
                self.function_call_streamer.mark_clean()

    def tool_calls_req(self) -> list[dict[str, Any]]:
        return [self.tool_calls[i] for i in sorted(self.tool_calls.keys())]

    def finalize_tool_calls(self) -> None:
        for i in sorted(self.tool_calls.keys()):
            tc = self.tool_calls[i]
            streamer = self.tool_args_streamers.get(i)
            if streamer and tc.get("arguments"):
                streamer.finalize_from_full(str(tc.get("arguments") or ""))
                self.content_complimentary = streamer.render()
                if self.notify_complimentary:
                    self.notify_complimentary(self.ctx, self.content_complimentary)
                streamer.mark_clean()

    def function_call_req(self) -> dict[str, Any] | None:
        if not (self.function_call_name or self.function_call_arguments):
            return None
        req = {"name": self.function_call_name, "arguments": self.function_call_arguments}
        if self.function_call_streamer and self.notify_complimentary and self.function_call_arguments:
            self.function_call_streamer.finalize_from_full(str(self.function_call_arguments or ""))
            self.content_complimentary = self.function_call_streamer.render()
            self.notify_complimentary(self.ctx, self.content_complimentary)
            self.function_call_streamer.mark_clean()
        return req

    def content_value(self) -> str | None:
        if self.message is None:
            return None
        return self.message.get("content")

    def refusal_value(self) -> str | None:
        if self.message is None:
            return None
        return self.message.get("refusal")

    def build_completion(self) -> ChatCompletion:
        choice_payload: dict[str, Any] = {
            "index": 0,
            "finish_reason": self.finish_reason,
            "message": {
                "role": (self.message or {}).get("role") or "assistant",
                "content": (self.message or {}).get("content") or None,
            },
        }
        for tc in self.tool_calls_req():
            choice_payload["message"].setdefault("tool_calls", []).append(
                {
                    "id": tc["id"],
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    "type": "function",
                }
            )
        fc_req = self.function_call_req()
        if fc_req:
            choice_payload["message"]["function_call"] = dict(fc_req)

        return ChatCompletion.model_validate(
            {
                "id": self.id_completion,
                "object": "chat.completion",
                "created": self.created,
                "model": self.model,
                "usage": self.usage,
                "choices": [choice_payload],
            }
        )
