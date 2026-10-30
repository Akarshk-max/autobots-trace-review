from typing import Any

from src.autobots.conn.gemini.chat_model_gemini import GeminiContextStoreRef
from src.autobots.conn.gemini.context_tool.chat_context_ingestion_policy import (
    GeminiChatContextIngestionPolicy,
    GeminiContextDocument,
)


class GeminiChatContextStoreManager:

    @staticmethod
    def ensure_chat_store_ref(
        *,
        existing_ref: GeminiContextStoreRef | dict[str, Any] | None,
        chat_id: str | None,
        display_name: str | None = None,
    ) -> GeminiContextStoreRef:
        if isinstance(existing_ref, GeminiContextStoreRef):
            ref = existing_ref
        elif isinstance(existing_ref, dict):
            ref = GeminiContextStoreRef.model_validate(existing_ref)
        else:
            ref = GeminiContextStoreRef(
                scope="chat_scoped",
                chat_id=chat_id,
                display_name=display_name or f"gemini-chat-context-{chat_id or 'default'}",
            )

        if chat_id and not getattr(ref, "chat_id", None):
            ref.chat_id = chat_id

        if not getattr(ref, "display_name", None):
            ref.display_name = display_name or f"gemini-chat-context-{ref.chat_id or 'default'}"
        elif chat_id and str(ref.display_name).startswith("gemini-chat-context-default"):
            ref.display_name = display_name or f"gemini-chat-context-{chat_id}"

        return ref

    @classmethod
    def build_documents_for_ingestion(
        cls,
        *,
        turn_history: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        pinned_messages: list[dict[str, Any]] | None,
        from_turn: int | None,
    ) -> list[GeminiContextDocument]:
        effective_pinned_messages = pinned_messages
        if from_turn is not None and int(from_turn) > 1:
            effective_pinned_messages = None

        turn_docs = GeminiChatContextIngestionPolicy.build_turn_documents(
            turn_history=turn_history,
            pinned_messages=effective_pinned_messages,
            from_turn=from_turn,
        )
        tool_docs = GeminiChatContextIngestionPolicy.build_tool_result_documents(
            tool_results=tool_results,
            from_turn=from_turn,
        )
        return [*turn_docs, *tool_docs]

    @classmethod
    def ingest_documents(
        cls,
        *,
        existing_ref: GeminiContextStoreRef | dict[str, Any] | None,
        chat_id: str | None,
        turn_history: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        pinned_messages: list[dict[str, Any]] | None,
        from_turn: int | None = None,
    ) -> tuple[GeminiContextStoreRef, list[GeminiContextDocument]]:
        ref = cls.ensure_chat_store_ref(existing_ref=existing_ref, chat_id=chat_id)
        documents = cls.build_documents_for_ingestion(
            turn_history=turn_history,
            tool_results=tool_results,
            pinned_messages=pinned_messages,
            from_turn=from_turn,
        )

        ref.metadata = {
            **dict(ref.metadata or {}),
            "pending_documents_count": len(documents),
            "prepared_from_turn": from_turn,
        }

        if turn_history:
            try:
                ref.last_indexed_turn = max(int(item.get("turn") or 0) for item in turn_history)
            except Exception:
                pass

        return ref, documents

    @staticmethod
    def search_documents_preview(
        *,
        documents: list[GeminiContextDocument],
        query: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        query_lower = query.lower().strip()
        matches: list[dict[str, Any]] = []
        for doc in documents:
            text_lower = doc.text.lower()
            score = 0.0
            if query_lower and query_lower in text_lower:
                score += 2.0
            for token in [piece for piece in query_lower.split() if piece]:
                if token in text_lower:
                    score += 0.25
            if score <= 0:
                continue
            matches.append(
                {
                    "title": doc.title,
                    "snippet": doc.text[:1200],
                    "score": score,
                    "metadata": doc.metadata,
                }
            )
        matches.sort(key=lambda item: item["score"], reverse=True)
        return matches[:top_k]
