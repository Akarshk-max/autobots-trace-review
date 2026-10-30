from typing import Any

from src.autobots.conn.gemini.context_tool.context_tool_models import (
    GeminiContextMatch,
    GeminiContextQueryReq,
)


class GeminiContextHistoryRetriever:
    @staticmethod
    def _normalize_role(role: str | None) -> str:
        role = str(role or "").lower()
        if role == "assistant":
            return "model"
        return role

    @classmethod
    def _message_text_haystack(cls, message: dict[str, Any]) -> str:
        parts = message.get("parts") or message.get("content", {}).get("parts") or []
        chunks: list[str] = [str(message.get("role") or "")]
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict):
                    for key in ("text", "name", "title"):
                        value = part.get(key)
                        if isinstance(value, str) and value:
                            chunks.append(value)
        return "\n".join(chunks).lower()

    @classmethod
    def _message_matches(cls, message: dict[str, Any], req: GeminiContextQueryReq) -> bool:
        if req.roles:
            role = cls._normalize_role(message.get("role"))
            allowed = {cls._normalize_role(r) for r in req.roles}
            if role not in allowed:
                return False

        if not req.query:
            return True

        haystack = cls._message_text_haystack(message)
        q = req.query.lower().strip()
        if q in haystack:
            return True
        if req.exact_match_only:
            return False
        return any(token in haystack for token in q.split() if token)

    @classmethod
    def _turn_matches(cls, turn: dict[str, Any], req: GeminiContextQueryReq) -> bool:
        turn_no = turn.get("turn")
        if req.turn is not None and turn_no != req.turn:
            return False
        if req.turns is not None and turn_no not in set(req.turns):
            return False
        if req.turn_min is not None and (turn_no is None or int(turn_no) < req.turn_min):
            return False
        if req.turn_max is not None and (turn_no is None or int(turn_no) > req.turn_max):
            return False

        if req.roles or req.query:
            for raw_message in turn.get("messages") or []:
                if isinstance(raw_message, dict) and cls._message_matches(raw_message, req):
                    return True
            return False
        return True

    @classmethod
    def _filtered_turn_payload(cls, turn: dict[str, Any], req: GeminiContextQueryReq) -> dict[str, Any]:
        if req.return_full_turn:
            return turn

        filtered_messages: list[dict[str, Any]] = []
        for raw_message in turn.get("messages") or []:
            if isinstance(raw_message, dict) and cls._message_matches(raw_message, req):
                filtered_messages.append(raw_message)

        return {
            "turn": turn.get("turn"),
            "messages": filtered_messages,
        }

    @classmethod
    def _turn_score(cls, turn: dict[str, Any], query: str | None) -> float:
        base = 1.0
        turn_no = turn.get("turn")
        try:
            if turn_no is not None:
                base += min(1.0, int(turn_no) / 1000.0)
        except Exception:
            pass

        if not query:
            return base

        query_lower = query.lower().strip()
        haystacks: list[str] = []
        for raw_msg in turn.get("messages") or []:
            if isinstance(raw_msg, dict):
                haystacks.append(cls._message_text_haystack(raw_msg))
        combined = "\n".join(haystacks)
        if not combined:
            return base

        if query_lower in combined:
            base += 2.0
        for token in [piece for piece in query_lower.split() if piece]:
            if token in combined:
                base += 0.25
        return base

    @classmethod
    def retrieve(
        cls,
        *,
        req: GeminiContextQueryReq,
    ) -> list[GeminiContextMatch]:
        matches: list[GeminiContextMatch] = []
        for turn in req.get_turn_history():
            if not cls._turn_matches(turn, req):
                continue

            payload = cls._filtered_turn_payload(turn, req)
            messages = payload.get("messages") or []
            snippet_parts: list[str] = []
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                parts = msg.get("parts") or []
                if isinstance(parts, list):
                    for part in parts:
                        if isinstance(part, dict):
                            text = part.get("text")
                            if isinstance(text, str) and text:
                                snippet_parts.append(text)
                if len(" ".join(snippet_parts)) > 500:
                    break

            snippet = "\n".join(snippet_parts).strip()[:1200] or None
            score = cls._turn_score(turn, req.query)
            matches.append(
                GeminiContextMatch(
                    source_type="turn_history",
                    score=score,
                    title=f"Turn {turn.get('turn')}",
                    snippet=snippet,
                    turn=turn.get("turn"),
                    record_id=f"turn:{turn.get('turn')}",
                    payload=payload,
                )
            )

        matches.sort(key=lambda item: item.score, reverse=True)
        return matches
