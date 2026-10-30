import json
from typing import Any

from pydantic import BaseModel

from src.autobots.conn.openrouter.utils import strip_reasoning_fields
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.chat_snapshot.chat_snapshot import store


def _jsonable(message: Any) -> Any:
    if hasattr(message, "model_dump"):
        try:
            return message.model_dump(exclude_none=True, mode="json")
        except Exception:
            pass
    return json.loads(json.dumps(message, default=str))


def _messages(config: BaseModel | dict[str, Any], field: str) -> list[Any]:
    raw = config.get(field) if isinstance(config, dict) else getattr(config, field, None)
    if not isinstance(raw, list):
        return []

    messages: list[Any] = []
    for message in raw:
        try:
            messages.append(_jsonable(message))
        except Exception:
            messages.append({"role": "assistant", "content": "[unserializable message]"})
    return messages


def _replace_messages_field(field: str):
    def apply(config: dict[str, Any], messages: list[Any], meta: dict[str, Any]) -> dict[str, Any]:
        config[field] = messages
        return config

    return apply


def _apply_turn_history(
    config: dict[str, Any], messages: list[Any], meta: dict[str, Any]
) -> dict[str, Any]:
    start_index = int(meta.get("turn_start_index") or 0)
    turn = int(meta.get("chat_turn") or 1)
    delta = messages[start_index:]
    if delta:
        history = list(config.get("turn_history") or [])
        history.append({"turn": turn, "messages": delta})
        config["turn_history"] = history
    config["messages"] = []
    return config


def _archive_entries(config: BaseModel | dict[str, Any]) -> list[Any] | None:
    raw = (
        config.get("input_archive")
        if isinstance(config, dict)
        else getattr(config, "input_archive", None)
    )
    if not isinstance(raw, list):
        return None

    entries: list[Any] = []
    for entry in raw:
        try:
            entries.append(_jsonable(entry))
        except Exception:
            return None
    return entries


def _apply_responses_archive(
    config: dict[str, Any], messages: list[Any], meta: dict[str, Any]
) -> dict[str, Any]:
    config["input"] = messages

    archive = meta.get("input_archive")
    if not isinstance(archive, list):
        archive = config.get("input_archive") or []
    archive = [dict(entry) if isinstance(entry, dict) else entry for entry in archive]

    if archive and messages and isinstance(archive[-1], dict):
        archive[-1]["output"] = list(archive[-1].get("output") or []) + [messages[-1]]
    if archive:
        config["input_archive"] = archive
    return config


def _claude_text_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    if not isinstance(content, list):
        return []
    return [
        block
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and str(block.get("text") or "").strip()
    ]


def _apply_claude_turn_history(
    config: dict[str, Any], messages: list[Any], meta: dict[str, Any]
) -> dict[str, Any]:
    start_index = int(meta.get("turn_start_index") or 0)
    turn = int(meta.get("chat_turn") or 1)

    user_messages: list[dict[str, Any]] = []
    assistant_texts: list[str] = []
    for message in messages[start_index:]:
        if not isinstance(message, dict):
            continue
        blocks = _claude_text_blocks(message.get("content"))
        if not blocks:
            continue
        if message.get("role") == "assistant":
            assistant_texts.extend(str(block.get("text")) for block in blocks)
        else:
            user_messages.append({"role": message.get("role"), "content": blocks})

    turn_messages = list(user_messages)
    if assistant_texts:
        turn_messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": "\n\n".join(assistant_texts)}]}
        )

    if turn_messages:
        history = list(config.get("turn_history") or [])
        history.append({"turn": turn, "messages": turn_messages})
        config["turn_history"] = history

    live_tool_results = meta.get("tool_results")
    if isinstance(live_tool_results, list):
        merged: list[Any] = []
        seen: set[str] = set()
        for item in list(config.get("tool_results") or []) + live_tool_results:
            if not isinstance(item, dict):
                continue
            identity = str(item.get("tool_use_id") or "")
            if not identity or identity in seen:
                continue
            seen.add(identity)
            merged.append(item)
        config["tool_results"] = merged

    config["messages"] = []
    return config


def _apply_openrouter_turn_history(
    config: dict[str, Any], messages: list[Any], meta: dict[str, Any]
) -> dict[str, Any]:
    from src.autobots.conn.openrouter.runtime_context import strip_runtime_context

    try:
        cleaned = strip_runtime_context(messages)
    except Exception:
        cleaned = messages

    start_index = int(meta.get("turn_start_index") or 0)
    turn = int(meta.get("chat_turn") or 1)
    delta = cleaned[start_index:]
    if delta:
        history = list(config.get("turn_history") or [])
        history.append({"turn": turn, "messages": delta})
        config["turn_history"] = history

    config["messages"] = cleaned
    return config


def _append_openrouter_notice(messages: list[Any], notice: str) -> list[Any]:
    messages.append({"role": "assistant", "content": notice})
    return messages


def _append_openai_chat_notice(messages: list[Any], notice: str) -> list[Any]:
    messages.append({"role": "assistant", "content": notice})
    return messages


def _append_deepseek_notice(messages: list[Any], notice: str) -> list[Any]:
    messages.append({"role": "assistant", "content": notice})
    return messages


def _append_grok_notice(messages: list[Any], notice: str) -> list[Any]:
    messages.append({"role": "assistant", "content": notice})
    return messages


def _append_claude_notice(messages: list[Any], notice: str) -> list[Any]:
    messages.append({"role": "assistant", "content": [{"type": "text", "text": notice}]})
    return messages


def _append_gemini_notice(messages: list[Any], notice: str) -> list[Any]:
    messages.append({"role": "model", "parts": [{"text": notice}]})
    return messages


def _append_responses_notice(messages: list[Any], notice: str) -> list[Any]:
    messages.append({"role": "assistant", "content": notice, "type": "message"})
    return messages


def snapshot_openrouter(
    ctx: Context,
    app_state: AppState,
    chat_req: BaseModel | dict[str, Any],
    loop_index: int,
    output_text: str = "",
) -> None:
    messages = [strip_reasoning_fields(message) for message in _messages(chat_req, "messages")]
    store(
        ctx,
        app_state,
        "openrouter",
        _apply_openrouter_turn_history,
        app_state.cancellation.snapshot_meta if app_state.cancellation else {},
        messages,
        _append_openrouter_notice,
        loop_index,
        output_text,
    )


def _claude_meta(app_state: AppState, chat_req: BaseModel | dict[str, Any]) -> dict[str, Any]:
    meta = dict(app_state.cancellation.snapshot_meta) if app_state.cancellation else {}
    raw = (
        chat_req.get("tool_results")
        if isinstance(chat_req, dict)
        else getattr(chat_req, "tool_results", None)
    )
    if isinstance(raw, list):
        try:
            meta["tool_results"] = [_jsonable(item) for item in raw]
        except Exception:
            pass
    return meta


def snapshot_claude(
    ctx: Context,
    app_state: AppState,
    chat_req: BaseModel | dict[str, Any],
    loop_index: int,
    output_text: str = "",
) -> None:
    store(
        ctx,
        app_state,
        "claude",
        _apply_claude_turn_history,
        _claude_meta(app_state, chat_req),
        _messages(chat_req, "messages"),
        _append_claude_notice,
        loop_index,
        output_text,
    )


def snapshot_openai_chat(
    ctx: Context,
    app_state: AppState,
    chat_req: BaseModel | dict[str, Any],
    loop_index: int,
    output_text: str = "",
) -> None:
    store(
        ctx,
        app_state,
        "openai_chat",
        _replace_messages_field("messages"),
        {},
        _messages(chat_req, "messages"),
        _append_openai_chat_notice,
        loop_index,
        output_text,
    )


def snapshot_openai_responses(
    ctx: Context,
    app_state: AppState,
    responses_req: BaseModel | dict[str, Any],
    loop_index: int,
    output_text: str = "",
) -> None:
    store(
        ctx,
        app_state,
        "openai_responses",
        _apply_responses_archive,
        {"input_archive": _archive_entries(responses_req)},
        _messages(responses_req, "input"),
        _append_responses_notice,
        loop_index,
        output_text,
    )


def snapshot_gemini(
    ctx: Context,
    app_state: AppState,
    chat_req: BaseModel | dict[str, Any],
    loop_index: int,
    output_text: str = "",
) -> None:
    store(
        ctx,
        app_state,
        "gemini",
        _apply_turn_history,
        app_state.cancellation.snapshot_meta if app_state.cancellation else {},
        _messages(chat_req, "messages"),
        _append_gemini_notice,
        loop_index,
        output_text,
    )


def snapshot_deepseek(
    ctx: Context,
    app_state: AppState,
    chat_req: BaseModel | dict[str, Any],
    loop_index: int,
    output_text: str = "",
) -> None:
    store(
        ctx,
        app_state,
        "deepseek",
        _replace_messages_field("messages"),
        {},
        _messages(chat_req, "messages"),
        _append_deepseek_notice,
        loop_index,
        output_text,
    )


def snapshot_grok(
    ctx: Context,
    app_state: AppState,
    chat_req: BaseModel | dict[str, Any],
    loop_index: int,
    output_text: str = "",
) -> None:
    store(
        ctx,
        app_state,
        "grok",
        _apply_turn_history,
        app_state.cancellation.snapshot_meta if app_state.cancellation else {},
        _messages(chat_req, "messages"),
        _append_grok_notice,
        loop_index,
        output_text,
    )
