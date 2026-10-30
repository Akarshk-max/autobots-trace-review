from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GeminiContextDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class GeminiChatContextIngestionPolicy:
    MAX_TEXT_PER_DOC = 12_000

    @staticmethod
    def _extract_message_text(message: dict[str, Any]) -> str:
        parts = message.get("parts") or message.get("content", {}).get("parts") or []
        chunks: list[str] = []
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        chunks.append(text)
        return "\n".join(chunks).strip()

    @classmethod
    def build_turn_documents(
        cls,
        *,
        turn_history: list[dict[str, Any]],
        pinned_messages: list[dict[str, Any]] | None = None,
        from_turn: int | None = None,
    ) -> list[GeminiContextDocument]:
        documents: list[GeminiContextDocument] = []

        if pinned_messages:
            text_chunks = [cls._extract_message_text(msg) for msg in pinned_messages if isinstance(msg, dict)]
            merged = "\n\n".join(chunk for chunk in text_chunks if chunk).strip()
            if merged:
                documents.append(
                    GeminiContextDocument(
                        title="Pinned conversation context",
                        text=merged[: cls.MAX_TEXT_PER_DOC],
                        metadata={"source_type": "pinned_messages"},
                    )
                )

        for turn in turn_history:
            if not isinstance(turn, dict):
                continue
            turn_no = turn.get("turn")
            if from_turn is not None:
                try:
                    if int(turn_no) < int(from_turn):
                        continue
                except Exception:
                    pass

            messages = turn.get("messages") or []
            rendered_blocks: list[str] = []
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                role = str(msg.get("role") or "unknown")
                text = cls._extract_message_text(msg)
                if text:
                    rendered_blocks.append(f"[{role}]\n{text}")

            rendered = "\n\n".join(rendered_blocks).strip()
            if not rendered:
                continue

            documents.append(
                GeminiContextDocument(
                    title=f"Chat turn {turn_no}",
                    text=rendered[: cls.MAX_TEXT_PER_DOC],
                    metadata={
                        "source_type": "turn_history",
                        "turn": turn_no,
                    },
                )
            )

        return documents

    @classmethod
    def build_tool_result_documents(
        cls,
        *,
        tool_results: list[dict[str, Any]],
        from_turn: int | None = None,
    ) -> list[GeminiContextDocument]:
        documents: list[GeminiContextDocument] = []
        for item in tool_results:
            if not isinstance(item, dict):
                continue

            chat_turn = item.get("chat_turn")
            if from_turn is not None:
                try:
                    if int(chat_turn) < int(from_turn):
                        continue
                except Exception:
                    pass

            output_text = str(item.get("output_text") or "").strip()
            if not output_text:
                continue

            name = str(item.get("name") or "tool")
            source = str(item.get("source") or "custom")
            text = (
                f"Tool name: {name}\n"
                f"Source: {source}\n"
                f"Chat turn: {chat_turn}\n"
                f"Output:\n{output_text}"
            )
            documents.append(
                GeminiContextDocument(
                    title=f"Tool result {name} turn {chat_turn}",
                    text=text[: cls.MAX_TEXT_PER_DOC],
                    metadata={
                        "source_type": "tool_results",
                        "turn": chat_turn,
                        "tool_name": name,
                        "tool_use_id": item.get("tool_use_id"),
                    },
                )
            )
        return documents
