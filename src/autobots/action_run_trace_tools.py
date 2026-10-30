"""Project Stage 1.5 tool events down to the fields each tool should keep.

The formatted trace stays intact. `apply_tool_wrappers` returns a copy whose
tool calls record the operation and whose tool results omit file bodies,
diffs, and command output.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

_SHORT = 200
_FILE_MUTATIONS = frozenset(
    {
        "write_file",
        "append_file",
        "replace_in_file",
        "move_path",
        "copy_path",
        "delete_path",
        "apply_unified_diff",
        "replace_line_range",
        "insert_at_line",
        "mkdir",
        "file_write_finish",
    }
)
_PATH_KEYS = (
    "path",
    "src_path",
    "root_path",
    "file_path",
    "source_file",
    "definition_file",
)
_QUERY_TOOLS = frozenset(
    {
        "search_web",
        "google_search_main",
        "perplexity_search",
        "exa_search",
        "read_web_url",
        "read_url",
        "video_search",
        "knowledge_base_query",
        "sandbox_file_query",
    }
)
_DOCUMENT_TOOLS = frozenset(
    {
        "pdf_parser",
        "pdf_highlight",
        "ocr_tool",
        "csv_xls_doc_ppt_to_txt",
        "csv_merge",
        "csv_merge_1",
        "merge_pdf",
        "json_reader",
        "json_flatten",
        "parquet_inspector",
        "csv_to_parquet",
    }
)
_STORE_TOOLS = frozenset(
    {
        "openai_vector_store_tool",
        "vector_store_handler",
        "gemini_file_search_store_tool",
    }
)
_CODE_TOOLS = frozenset({"daytona_sandbox", "gemini_code_execution_tool"})
_ASSISTANT_TOOLS = frozenset({"file_search_tool", "coding_assistant", "openai_assistant"})

Projector = Callable[[dict[str, Any], Any], dict[str, Any]]


def apply_tool_wrappers(formatted: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a Stage 1.5 trace with tool bodies replaced by effects."""
    if not isinstance(formatted, dict):
        raise TypeError("formatted trace must be a dict")
    view = copy.deepcopy(formatted)
    events = view.get("events")
    if not isinstance(events, list):
        return view
    results = [
        event
        for event in events
        if isinstance(event, dict) and event.get("kind") == "tool_result"
    ]
    for event in events:
        if not isinstance(event, dict) or event.get("kind") != "tool_call":
            continue
        result = _paired_result(event, results)
        effect = project_tool_call(event.get("name"), event.get("arguments"), _result_body(result))
        event.pop("arguments", None)
        event.pop("function_fields", None)
        event.pop("call_fields", None)
        event["effect"] = effect
        if result is not None:
            _omit_result_body(result)
    for event in results:
        if event.get("body_omitted") is True:
            continue
        body = event.get("content")
        _omit_result_body(event)
        if event.get("name"):
            event["effect"] = project_tool_call(event.get("name"), None, body)
    return view


def project_tool_call(name: Any, arguments: Any, result: Any) -> dict[str, Any]:
    """Return the trace record for one known tool call."""
    args = _as_dict(arguments)
    handler = _HANDLERS.get(name)
    if handler is None:
        return _default(name, args, result)
    effect = handler(args, result)
    if name and "tool" not in effect:
        effect["tool"] = name
    return effect


def _paired_result(call: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any] | None:
    message_index = call.get("result_message_index")
    if isinstance(message_index, int):
        for event in results:
            if event.get("message_index") == message_index:
                return event
    call_id = call.get("call_id")
    if call_id is None:
        return None
    for event in results:
        if event.get("call_id") == call_id:
            return event
    return None


def _result_body(result: dict[str, Any] | None) -> Any:
    if result is None:
        return None
    return result.get("content")


def _omit_result_body(event: dict[str, Any]) -> None:
    for key in (
        "content",
        "external",
        "truncated",
        "unresolved",
        "complete",
        "content_source",
    ):
        event.pop(key, None)
    missing = [item for item in event.get("missing") or [] if item != "external_content"]
    if missing:
        event["missing"] = missing
    else:
        event.pop("missing", None)
    event["body_omitted"] = True


def _file_system(args: dict[str, Any], result: Any) -> dict[str, Any]:
    payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
    operation = args.get("operation")
    effect: dict[str, Any] = {"tool": "file_system_tool", "body_omitted": True}
    if isinstance(operation, str):
        effect["operation"] = operation
    paths = [payload.get(key) for key in _PATH_KEYS if isinstance(payload.get(key), str)]
    if paths:
        effect["path"] = paths[0]
    if len(paths) > 1:
        effect["paths"] = paths
    if isinstance(payload.get("dst_path"), str):
        effect["destination"] = payload["dst_path"]
    for key in ("query", "symbol"):
        if _is_short(payload.get(key)):
            effect[key] = payload[key]
    for key in ("start_line", "end_line", "line_number"):
        if isinstance(payload.get(key), int):
            effect[key] = payload[key]
    if operation == "batch":
        steps = _batch_steps(payload.get("ops"))
        if steps:
            effect["steps"] = steps
    effect.update(_status(result))
    if operation in _FILE_MUTATIONS and effect.get("success") is True:
        effect["changed"] = True
    elif operation in _FILE_MUTATIONS and effect.get("success") is False:
        effect["changed"] = False
    return effect


def _batch_steps(ops: Any) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if not isinstance(ops, list):
        return steps
    for op in ops[:30]:
        if not isinstance(op, dict):
            continue
        step: dict[str, Any] = {}
        if isinstance(op.get("operation"), str):
            step["operation"] = op["operation"]
        inner = op.get("payload") if isinstance(op.get("payload"), dict) else op
        if isinstance(inner.get("path"), str):
            step["path"] = inner["path"]
        if step:
            steps.append(step)
    return steps


def _terminal(args: dict[str, Any], result: Any) -> dict[str, Any]:
    effect: dict[str, Any] = {"tool": "run_terminal_command", "body_omitted": True}
    _put_short_or_omit(effect, "command", args.get("command"))
    if _is_short(args.get("session_id")):
        effect["session_id"] = args["session_id"]
    effect.update(_status(result))
    parsed = _as_dict(result)
    if "session_id" not in effect and _is_short(parsed.get("session_id")):
        effect["session_id"] = parsed["session_id"]
    return effect


def _read_files(args: dict[str, Any], result: Any) -> dict[str, Any]:
    effect: dict[str, Any] = {"tool": "read_files_from_user_system", "body_omitted": True}
    paths = args.get("paths")
    if isinstance(paths, list):
        effect["paths"] = [path for path in paths if isinstance(path, str)][:20]
    parsed = _as_dict(result)
    if isinstance(parsed.get("urls"), list):
        effect["uploaded_count"] = len(parsed["urls"])
    effect.update(_status(result))
    return effect


def _pty_start(args: dict[str, Any], result: Any) -> dict[str, Any]:
    effect: dict[str, Any] = {"tool": "start_pty_session", "body_omitted": True}
    _put_short_or_omit(effect, "command", args.get("command"))
    effect.update(_status(result))
    parsed = _as_dict(result)
    if _is_short(parsed.get("session_id")):
        effect["session_id"] = parsed["session_id"]
    return effect


def _pty_send(args: dict[str, Any], result: Any) -> dict[str, Any]:
    effect: dict[str, Any] = {"tool": "send_pty_input", "body_omitted": True}
    if _is_short(args.get("session_id")):
        effect["session_id"] = args["session_id"]
    data = args.get("input_data")
    if isinstance(data, str) and len(data) <= 40:
        effect["input_data"] = data
    elif isinstance(data, str):
        effect["input_omitted"] = True
    effect.update(_status(result))
    return effect


def _pty_read(args: dict[str, Any], result: Any) -> dict[str, Any]:
    effect: dict[str, Any] = {"tool": "read_pty_output", "body_omitted": True}
    if _is_short(args.get("session_id")):
        effect["session_id"] = args["session_id"]
    effect.update(_status(result))
    return effect


def _pty_close(args: dict[str, Any], result: Any) -> dict[str, Any]:
    effect: dict[str, Any] = {"tool": "close_pty_session", "body_omitted": True}
    if _is_short(args.get("session_id")):
        effect["session_id"] = args["session_id"]
    effect.update(_status(result))
    return effect


def _save_files(args: dict[str, Any], result: Any) -> dict[str, Any]:
    effect: dict[str, Any] = {"tool": "save_files_tool", "body_omitted": True}
    names = _file_names(args.get("files"))
    if names:
        effect["files"] = names
    effect.update(_status(result))
    return effect


def _assistant(args: dict[str, Any], result: Any) -> dict[str, Any]:
    effect: dict[str, Any] = {"body_omitted": True}
    if isinstance(args.get("urls"), list):
        effect["url_count"] = len(args["urls"])
    if isinstance(args.get("instructions"), str):
        effect["instructions_omitted"] = True
    effect.update(_status(result))
    return effect


def _query(args: dict[str, Any], result: Any) -> dict[str, Any]:
    effect: dict[str, Any] = {"body_omitted": True}
    for key in ("query", "q", "url", "search_term"):
        if _is_short(args.get(key)):
            effect[key] = args[key]
    if "query" not in effect and _is_short(args.get("text")):
        effect["query"] = args["text"]
    if isinstance(args.get("urls"), list):
        effect["url_count"] = len(args["urls"])
    effect.update(_status(result))
    return effect


def _document(args: dict[str, Any], result: Any) -> dict[str, Any]:
    effect: dict[str, Any] = {"body_omitted": True}
    for key in ("pdf_url", "url", "path", "filename", "file_url"):
        if _is_short(args.get(key)):
            effect[key] = args[key]
    paths = args.get("file_paths")
    if isinstance(paths, list):
        effect["paths"] = [path for path in paths if isinstance(path, str)][:20]
    if isinstance(args.get("urls"), list):
        effect["url_count"] = len(args["urls"])
    effect.update(_status(result))
    return effect


def _store(args: dict[str, Any], result: Any) -> dict[str, Any]:
    effect: dict[str, Any] = {"body_omitted": True}
    if isinstance(args.get("operation"), str):
        effect["operation"] = args["operation"]
    if _is_short(args.get("vector_store_id")):
        effect["vector_store_id"] = args["vector_store_id"]
    if _is_short(args.get("query")):
        effect["query"] = args["query"]
    if isinstance(args.get("files"), list):
        effect["file_count"] = len(args["files"])
    effect.update(_status(result))
    return effect


def _code(args: dict[str, Any], result: Any) -> dict[str, Any]:
    effect: dict[str, Any] = {"body_omitted": True}
    if isinstance(args.get("operation"), str):
        effect["operation"] = args["operation"]
    _put_short_or_omit(effect, "command", args.get("command"))
    if _is_short(args.get("path")):
        effect["path"] = args["path"]
    if _is_short(args.get("language")):
        effect["language"] = args["language"]
    effect.update(_status(result))
    return effect


def _default(name: Any, args: dict[str, Any], result: Any) -> dict[str, Any]:
    effect: dict[str, Any] = {"body_omitted": True}
    if isinstance(name, str) and name:
        effect["tool"] = name
    for key in ("operation", "path", "url", "query", "name", "id", "action_id", "session_id"):
        if _is_short(args.get(key)):
            effect[key] = args[key]
    _put_short_or_omit(effect, "command", args.get("command"))
    effect.update(_status(result))
    return effect


def _status(result: Any) -> dict[str, Any]:
    data = _as_dict(result)
    found: dict[str, Any] = {}
    if isinstance(data.get("success"), bool):
        found["success"] = data["success"]
    if _is_short(data.get("status")):
        found["status"] = data["status"]
    if isinstance(data.get("exit_code"), int):
        found["exit_code"] = data["exit_code"]
    return found


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _is_short(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _SHORT


def _put_short_or_omit(effect: dict[str, Any], key: str, value: Any) -> None:
    if not isinstance(value, str) or value == "":
        return
    if len(value) <= _SHORT:
        effect[key] = value
        return
    effect[f"{key}_omitted"] = True


def _file_names(files: Any) -> list[str]:
    names: list[str] = []
    if not isinstance(files, list):
        return names
    for item in files[:20]:
        url = item.get("url") if isinstance(item, dict) else item if isinstance(item, str) else None
        if not isinstance(url, str) or not url:
            continue
        name = url.split("?")[0].rstrip("/").split("/")[-1]
        if name:
            names.append(name)
    return names


def _handlers() -> dict[str, Projector]:
    handlers: dict[str, Projector] = {
        "file_system_tool": _file_system,
        "run_terminal_command": _terminal,
        "read_files_from_user_system": _read_files,
        "start_pty_session": _pty_start,
        "send_pty_input": _pty_send,
        "read_pty_output": _pty_read,
        "close_pty_session": _pty_close,
        "save_files_tool": _save_files,
    }
    for name in _ASSISTANT_TOOLS:
        handlers[name] = _assistant
    for name in _QUERY_TOOLS:
        handlers[name] = _query
    for name in _DOCUMENT_TOOLS:
        handlers[name] = _document
    for name in _STORE_TOOLS:
        handlers[name] = _store
    for name in _CODE_TOOLS:
        handlers[name] = _code
    return handlers


_HANDLERS = _handlers()
