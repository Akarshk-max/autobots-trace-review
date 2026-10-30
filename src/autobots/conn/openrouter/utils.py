from typing import Any


def to_plain(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)
    if isinstance(obj, list):
        return [to_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    return obj


def dedupe_urls(urls: list[str] | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in urls or []:
        url = str(raw or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def strip_reasoning_fields(message: Any) -> Any:
    if isinstance(message, dict):
        message.pop("reasoning", None)
        message.pop("reasoning_details", None)
    return message


def is_tool_present(tools: list[Any] | None, name: str) -> bool:
    for tool in tools or []:
        if isinstance(tool, str) and tool == name:
            return True
        if isinstance(tool, dict):
            if tool.get("name") == name:
                return True
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
            if fn.get("name") == name:
                return True
    return False
