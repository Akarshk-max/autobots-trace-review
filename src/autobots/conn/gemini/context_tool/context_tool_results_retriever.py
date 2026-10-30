from typing import Any

from src.autobots.conn.gemini.context_tool.context_tool_models import (
    GeminiContextMatch,
    GeminiContextQueryReq,
)


class GeminiContextToolResultsRetriever:
    @staticmethod
    def _result_haystack(item: dict[str, Any]) -> str:
        chunks: list[str] = [
            str(item.get("name") or ""),
            str(item.get("source") or ""),
            str(item.get("output_text") or ""),
        ]
        input_payload = item.get("input")
        if isinstance(input_payload, dict):
            chunks.extend(str(v) for v in input_payload.values() if v is not None)
        return "\n".join(chunks).lower()

    @classmethod
    def _matches_filters(cls, item: dict[str, Any], req: GeminiContextQueryReq) -> bool:
        chat_turn = item.get("chat_turn")
        if req.turn is not None and chat_turn != req.turn:
            return False
        if req.turns is not None and chat_turn not in set(req.turns):
            return False
        if req.turn_min is not None and (chat_turn is None or int(chat_turn) < req.turn_min):
            return False
        if req.turn_max is not None and (chat_turn is None or int(chat_turn) > req.turn_max):
            return False
        if req.tool_names and str(item.get("name") or "") not in set(req.tool_names):
            return False
        if not req.include_error_results and bool(item.get("is_error")):
            return False
        if req.has_generated_files is True and not (item.get("generated_files") or []):
            return False
        if req.has_generated_files is False and (item.get("generated_files") or []):
            return False
        if req.has_urls is True and not (item.get("urls") or []):
            return False
        if req.has_urls is False and (item.get("urls") or []):
            return False
        if req.query:
            haystack = cls._result_haystack(item)
            q = req.query.lower().strip()
            if q not in haystack:
                if req.exact_match_only:
                    return False
                if not any(token in haystack for token in q.split() if token):
                    return False
        return True

    @classmethod
    def _score(cls, item: dict[str, Any], req: GeminiContextQueryReq) -> float:
        score = 1.0
        if bool(item.get("is_error")):
            score -= 0.2
        if req.query:
            haystack = cls._result_haystack(item)
            q = req.query.lower().strip()
            if q in haystack:
                score += 2.0
            for token in [piece for piece in q.split() if piece]:
                if token in haystack:
                    score += 0.25
        try:
            score += min(1.0, int(item.get("chat_turn") or 0) / 1000.0)
        except Exception:
            pass
        return score

    @classmethod
    def retrieve(
        cls,
        *,
        req: GeminiContextQueryReq,
    ) -> list[GeminiContextMatch]:
        matches: list[GeminiContextMatch] = []
        for item in req.get_tool_results():
            if not cls._matches_filters(item, req):
                continue

            snippet = str(item.get("output_text") or "").strip()[:1200] or None
            matches.append(
                GeminiContextMatch(
                    source_type="tool_results",
                    score=cls._score(item, req),
                    title=f"Tool result: {item.get('name')}",
                    snippet=snippet,
                    turn=item.get("chat_turn"),
                    tool_name=item.get("name"),
                    record_id=f"tool:{item.get('tool_use_id')}",
                    payload=item,
                )
            )

        matches.sort(key=lambda item: item.score, reverse=True)
        return matches
