import json
from typing import Any

from src.autobots.conn.gemini.context_tool.context_history_retriever import (
    GeminiContextHistoryRetriever,
)
from src.autobots.conn.gemini.context_tool.context_tool_models import (
    GeminiContextMatch,
    GeminiContextQueryReq,
    GeminiContextQueryRes,
    GeminiContextToolStats,
)
from src.autobots.conn.gemini.context_tool.context_tool_results_retriever import (
    GeminiContextToolResultsRetriever,
)
from src.autobots.conn.gemini.file_search_store.file_search_store_client import (
    GeminiFileSearchStoreClient,
)
from src.autobots.conn.gemini.message_normalizer_gemini import GeminiMessageNormalizer


class GeminiContextToolService:
    @staticmethod
    def build_stats(req: GeminiContextQueryReq) -> GeminiContextToolStats:
        return GeminiContextToolStats(
            turn_history_count=len(req.get_turn_history()),
            tool_results_count=len(req.get_tool_results()),
            pinned_messages_count=len(req.get_pinned_messages()),
        )

    @classmethod
    def _project_payload(cls, payload: dict[str, Any], fields: list[str] | None) -> dict[str, Any]:
        if not fields:
            return payload
        return {field: payload.get(field) for field in fields}

    @staticmethod
    def _sort_matches(matches: list[GeminiContextMatch], req: GeminiContextQueryReq) -> list[GeminiContextMatch]:
        if req.sort_by == "turn":
            return sorted(
                matches,
                key=lambda item: (int(item.turn or -1), item.score),
                reverse=req.descending,
            )
        if req.sort_by == "tool_name":
            return sorted(
                matches,
                key=lambda item: (str(item.tool_name or ""), item.score),
                reverse=req.descending,
            )
        if req.sort_by == "source_type":
            return sorted(
                matches,
                key=lambda item: (str(item.source_type), item.score),
                reverse=req.descending,
            )
        return sorted(matches, key=lambda item: item.score, reverse=req.descending)

    @classmethod
    def _merge_matches(
        cls,
        *,
        history_matches: list[GeminiContextMatch],
        tool_matches: list[GeminiContextMatch],
        chat_store_matches: list[GeminiContextMatch],
        req: GeminiContextQueryReq,
    ) -> list[GeminiContextMatch]:
        merged = [*history_matches, *tool_matches, *chat_store_matches]
        return cls._sort_matches(merged, req)


    @classmethod
    def _chat_store_matches_from_response(cls, response: Any) -> list[GeminiContextMatch]:
        plain = GeminiMessageNormalizer.normalize_response(response)
        candidates = plain.get("candidates") or []
        if not isinstance(candidates, list) or not candidates:
            return []
        first = candidates[0] if isinstance(candidates[0], dict) else {}
        grounding = first.get("grounding_metadata") or first.get("groundingMetadata") or {}
        chunks = grounding.get("grounding_chunks") or grounding.get("groundingChunks") or []
        matches: list[GeminiContextMatch] = []
        for idx, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                continue
            retrieved = chunk.get("retrieved_context") or chunk.get("retrievedContext") or {}
            if not isinstance(retrieved, dict):
                continue
            snippet = retrieved.get("text")
            title = retrieved.get("title") or f"Chat store match {idx + 1}"
            custom_metadata = retrieved.get("custom_metadata") or retrieved.get("customMetadata") or []
            metadata = {}
            if isinstance(custom_metadata, list):
                for item in custom_metadata:
                    if not isinstance(item, dict):
                        continue
                    key = item.get("key")
                    if not key:
                        continue
                    metadata[str(key)] = item.get("string_value") or item.get("stringValue") or item.get("numeric_value") or item.get("numericValue")
            matches.append(
                GeminiContextMatch(
                    source_type="chat_store",
                    score=1.5 - min(idx * 0.05, 1.0),
                    title=title,
                    snippet=snippet,
                    turn=metadata.get("turn") if isinstance(metadata.get("turn"), int) else None,
                    tool_name=metadata.get("tool_name") if isinstance(metadata.get("tool_name"), str) else None,
                    record_id=retrieved.get("uri") or f"chat_store:{idx}",
                    payload={
                        "retrieved_context": retrieved,
                        "metadata": metadata,
                    },
                )
            )
        if matches:
            return matches

        response_text = getattr(response, "text", None)
        if isinstance(response_text, str) and response_text.strip():
            return [
                GeminiContextMatch(
                    source_type="chat_store",
                    score=1.0,
                    title="Chat store semantic answer",
                    snippet=response_text[:2000],
                    record_id="chat_store:answer",
                    payload={"text": response_text, "response": plain},
                )
            ]
        return []


    @classmethod
    async def execute(
        cls,
        req: GeminiContextQueryReq,
        *,
        fs_client: GeminiFileSearchStoreClient | None = None,
        model: str | None = None,
    ) -> GeminiContextQueryRes:
        stats = cls.build_stats(req)

        if req.operation == "stats":
            return GeminiContextQueryRes(
                operation=req.operation,
                summary="Context tool stats retrieved.",
                stats=stats,
            )

        if req.operation == "get_turns":
            history_matches = GeminiContextHistoryRetriever.retrieve(req=req)
            paged = history_matches[req.offset : req.offset + req.limit]
            return GeminiContextQueryRes(
                operation=req.operation,
                summary=(
                    "Use get_turns when you need exact ground truth from persisted conversation turns. "
                    f"Found {len(history_matches)} matching turns."
                ),
                total_count=len(history_matches),
                returned_count=len(paged),
                matches=[
                    match.model_copy(update={"payload": cls._project_payload(match.payload, req.fields)})
                    for match in paged
                ],
                stats=stats,
            )

        if req.operation == "get_tool_results":
            tool_matches = GeminiContextToolResultsRetriever.retrieve(req=req)
            paged = tool_matches[req.offset : req.offset + req.limit]
            return GeminiContextQueryRes(
                operation=req.operation,
                summary=(
                    "Use get_tool_results when you need exact ground truth from persisted tool outputs. "
                    f"Found {len(tool_matches)} matching tool results."
                ),
                total_count=len(tool_matches),
                returned_count=len(paged),
                matches=[
                    match.model_copy(update={"payload": cls._project_payload(match.payload, req.fields)})
                    for match in paged
                ],
                stats=stats,
            )

    @classmethod
    def result_to_text(cls, result: GeminiContextQueryRes) -> str:
        return json.dumps(result.model_dump(exclude_none=True), indent=2, ensure_ascii=False)