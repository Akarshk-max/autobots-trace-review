"""Stage 1 stitches an S3 action_run. Stage 1.5 formats that trace for an LLM.

`join_action_run` keeps a deep copy of the source document. `execution` records
order and pairing. `format_trace_for_llm` derives a chronological view. It does
not summarize, extract a skill, or modify the Stage 1 object.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.autobots.action_run_trace_tools import apply_tool_wrappers

_SKILL_PATH = Path(__file__).with_name("action_run_trace_skill.md")
_OUTPUT_FILE_MARKER = "Output stored in file url: "

ExternalRetriever = Callable[[str], str | None]


def load_skill() -> str:
    return _SKILL_PATH.read_text(encoding="utf-8")


def join_action_run(
    document: dict[str, Any],
    *,
    retrieve_external: ExternalRetriever | None = None,
) -> dict[str, Any]:
    """Return a lossless canonical trace for one ActionResultDoc object.

    `retrieve_external` may read a tool-output URL. When it is omitted, file
    references stay unresolved and `complete` is false.
    """
    if not isinstance(document, dict):
        raise TypeError("action run document must be a dict")
    source = copy.deepcopy(document)
    result = source.get("result") if isinstance(source.get("result"), dict) else {}
    config = result.get("config") if isinstance(result.get("config"), dict) else {}
    messages = config.get("messages") if isinstance(config.get("messages"), list) else []
    recorded = result.get("results") if isinstance(result.get("results"), list) else []
    run = {key: value for key, value in source.items() if key != "result"}
    return {
        "run": run,
        "result": result,
        "execution": _execution(messages, recorded),
        "external_references": _external_references(result, retrieve_external),
    }


def build_openrouter_request(
    document: dict[str, Any],
    *,
    model: str,
    skill: str | None = None,
    retrieve_external: ExternalRetriever | None = None,
) -> dict[str, Any]:
    """Chat-completions body: system message is the skill, user message is Stage 1.5.

    The Stage 1 trace is formatted, then tool wrappers replace file bodies and
    command output. Only that projected object is serialized into the user message.
    """
    if not model:
        raise ValueError("model is required")
    trace = join_action_run(document, retrieve_external=retrieve_external)
    formatted = apply_tool_wrappers(format_trace_for_llm(trace))
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": load_skill() if skill is None else skill},
            {"role": "user", "content": json.dumps(formatted, ensure_ascii=False)},
        ],
    }


def format_trace_for_llm(trace: dict[str, Any]) -> dict[str, Any]:
    """Return a chronological view of a Stage 1 trace.

    The trace is read only. Exact text matches are collapsed. Differing text,
    files, and multimodal parts stay in the event that owns them.
    """
    if not isinstance(trace, dict):
        raise TypeError("trace must be a dict")
    return {"context": _format_context(trace), "events": _format_events(trace)}


def _execution(messages: list[Any], recorded: list[Any]) -> dict[str, Any]:
    matches = _match_results(messages, recorded)
    turns = _turns(messages, matches)
    for number, turn in enumerate(turns, start=1):
        turn["turn"] = number
    return {"turns": turns, "tool_links": _tool_links(messages)}


def _match_results(messages: list[Any], recorded: list[Any]) -> list[int | None]:
    matches: list[int | None] = []
    cursor = 0
    for item in recorded:
        user_input = item.get("input") if isinstance(item, dict) else None
        if not isinstance(user_input, dict):
            matches.append(None)
            continue
        expected = _stored_user_text(user_input)
        raw_text = user_input.get("text") or ""
        found: int | None = None
        for index in range(cursor, len(messages)):
            if _is_turn_start(messages[index], expected, raw_text):
                found = index
                break
        matches.append(found)
        if found is not None:
            cursor = found + 1
    return matches


def _turns(messages: list[Any], matches: list[int | None]) -> list[dict[str, Any]]:
    paired = {index: message_index for index, message_index in enumerate(matches) if message_index is not None}
    message_owner = {message_index: result_index for result_index, message_index in paired.items()}
    cuts = sorted(message_owner)
    spans: list[tuple[int, int, int | None]] = []
    previous = 0
    for cut in cuts:
        if previous < cut:
            spans.append((previous, cut, None))
        later = [point for point in cuts if point > cut]
        end = later[0] if later else len(messages)
        spans.append((cut, end, message_owner[cut]))
        previous = end
    if previous < len(messages):
        spans.append((previous, len(messages), None))
    if not cuts and messages:
        spans = [(0, len(messages), None)]

    turns: list[dict[str, Any]] = []
    emitted: set[int] = set()

    def emit_unmatched_results(before_result: int | None) -> None:
        for result_index, message_index in enumerate(matches):
            if message_index is not None or result_index in emitted:
                continue
            next_paired = next((index for index in sorted(paired) if index > result_index), None)
            if next_paired == before_result or (before_result is None and next_paired is None):
                turns.append(_turn(result_index, None, [], "unmatched_result"))
                emitted.add(result_index)

    for start, end, result_index in spans:
        if result_index is None:
            turns.append(_turn(None, None, list(range(start, end)), "unmatched_messages"))
            continue
        emit_unmatched_results(result_index)
        turns.append(_turn(result_index, start, list(range(start, end)), "paired"))
        emitted.add(result_index)
    emit_unmatched_results(None)
    return turns


def _turn(
    result_index: int | None,
    user_message_index: int | None,
    message_indexes: list[int],
    match: str,
) -> dict[str, Any]:
    return {
        "result_index": result_index,
        "match": match,
        "user_message_index": user_message_index,
        "message_indexes": message_indexes,
    }


def _tool_links(messages: list[Any]) -> list[dict[str, Any]]:
    pending: dict[Any, list[dict[str, Any]]] = {}
    links: list[dict[str, Any]] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
            for call_index, call in enumerate(calls):
                if not isinstance(call, dict):
                    continue
                link = {
                    "call_id": call.get("id"),
                    "call_message_index": message_index,
                    "call_index": call_index,
                    "result_message_index": None,
                    "status": "unmatched_call",
                }
                links.append(link)
                if call.get("id") is not None:
                    pending.setdefault(call.get("id"), []).append(link)
            continue
        if role not in {"tool", "function"}:
            continue
        call_id = message.get("tool_call_id")
        if call_id is None:
            call_id = message.get("name")
        queue = pending.get(call_id) or []
        if queue:
            link = queue.pop(0)
            link["result_message_index"] = message_index
            link["status"] = "paired"
            continue
        links.append(
            {
                "call_id": call_id,
                "call_message_index": None,
                "call_index": None,
                "result_message_index": message_index,
                "status": "unmatched_result",
            }
        )
    return links


def _is_turn_start(message: Any, expected: str, raw_text: str) -> bool:
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = _message_text(message.get("content"))
    if expected and content == expected:
        return True
    return bool(raw_text) and content == raw_text


def _stored_user_text(user_input: dict[str, Any]) -> str:
    text = user_input.get("text") or ""
    urls = user_input.get("urls") or []
    if urls:
        base = text if text else " "
        return f"{base}\nUser-uploaded file URLs: {urls}"
    return text


def _message_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
        return "\n".join(part for part in parts if part)
    return str(content)


def _external_references(
    result: dict[str, Any],
    retrieve_external: ExternalRetriever | None,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    _collect_external(result, "result", found, retrieve_external)
    return found


def _collect_external(
    value: Any,
    path: str,
    found: list[dict[str, Any]],
    retrieve_external: ExternalRetriever | None,
) -> None:
    if isinstance(value, str):
        reference = _reference_from_text(value, path, retrieve_external)
        if reference is not None:
            found.append(reference)
        return
    if isinstance(value, dict):
        for key, child in value.items():
            _collect_external(child, f"{path}.{key}", found, retrieve_external)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _collect_external(child, f"{path}[{index}]", found, retrieve_external)


def _reference_from_text(
    text: str,
    path: str,
    retrieve_external: ExternalRetriever | None,
) -> dict[str, Any] | None:
    if _OUTPUT_FILE_MARKER not in text:
        return None
    preview, _, url = text.partition(_OUTPUT_FILE_MARKER)
    file_url = url.strip()
    retrieved_text: str | None = None
    if not file_url:
        status = "missing_url"
    elif retrieve_external is None:
        status = "not_attempted"
    else:
        try:
            retrieved_text = retrieve_external(file_url)
        except Exception:
            status = "failed"
        else:
            status = "retrieved" if retrieved_text is not None else "not_retrieved"
    return {
        "location": path,
        "available_preview": preview,
        "file_url": file_url or None,
        "retrieval_status": status,
        "retrieved_text": retrieved_text,
        "complete": status == "retrieved",
    }


_SETUP_ROLES = frozenset({"system", "developer"})
_TOOL_ROLES = frozenset({"tool", "function"})
_NAMED_RUN_KEYS = (
    "name",
    "status",
    "error_message",
    "created_at",
    "updated_at",
    "file_url",
    "batch_id",
    "parent_action_result_id",
)
_MESSAGE_RESERVED = frozenset(
    {"role", "content", "tool_calls", "tool_call_id", "name", "function_call"}
)
_PAYLOAD_KEYS = frozenset(
    {
        "text",
        "image_url",
        "url",
        "file_id",
        "file_data",
        "data",
        "source",
        "input_audio",
        "input_image",
        "image",
    }
)
_MESSAGE_LOCATION = re.compile(r"^result\.config\.messages\[(\d+)\]\.content(.*)$")
_BLOCK_LOCATION = re.compile(r"^result\.results\[(\d+)\]\.output\.blocks\[(\d+)\]\.text$")
_INPUT_LOCATION = re.compile(r"^result\.results\[(\d+)\]\.input(.*)$")
_PATH_TOKEN = re.compile(r"\[(\d+)\]|\.([^.\[\]]+)")


def _format_context(trace: dict[str, Any]) -> dict[str, Any]:
    run = trace.get("run") if isinstance(trace.get("run"), dict) else {}
    result = trace.get("result") if isinstance(trace.get("result"), dict) else {}
    config = result.get("config") if isinstance(result.get("config"), dict) else {}
    context: dict[str, Any] = {}
    used: set[str] = set()
    if "id" in run:
        context["run_id"] = copy.deepcopy(run["id"])
        used.add("id")
        if "_id" in run and run["_id"] == run["id"]:
            used.add("_id")
    elif "_id" in run:
        context["run_id"] = copy.deepcopy(run["_id"])
        used.add("_id")
    for key in _NAMED_RUN_KEYS:
        if key in run:
            context[key] = copy.deepcopy(run[key])
            used.add(key)
    if "type" in result:
        context["action_type"] = copy.deepcopy(result["type"])
    if "id" in result:
        context["action_id"] = copy.deepcopy(result["id"])
    if "model" in config:
        context["model"] = copy.deepcopy(config["model"])
    if "provider" in config:
        context["provider"] = copy.deepcopy(config["provider"])
    if "tools" in config:
        context["available_tools"] = copy.deepcopy(config["tools"])
    other_run = {key: copy.deepcopy(value) for key, value in run.items() if key not in used}
    if other_run:
        context["other_run_fields"] = other_run
    other_config = {
        key: copy.deepcopy(value)
        for key, value in config.items()
        if key not in {"messages", "tools", "model", "provider"}
    }
    if other_config:
        context["other_config"] = other_config
    return context


def _format_events(trace: dict[str, Any]) -> list[dict[str, Any]]:
    result = trace.get("result") if isinstance(trace.get("result"), dict) else {}
    config = result.get("config") if isinstance(result.get("config"), dict) else {}
    messages = config.get("messages") if isinstance(config.get("messages"), list) else []
    recorded = result.get("results") if isinstance(result.get("results"), list) else []
    execution = trace.get("execution") if isinstance(trace.get("execution"), dict) else {}
    raw_spans = execution.get("turns") if isinstance(execution.get("turns"), list) else []
    spans = [span for span in raw_spans if isinstance(span, dict)]
    if not spans and messages:
        spans = [
            {
                "match": "unmatched_messages",
                "result_index": None,
                "user_message_index": None,
                "message_indexes": list(range(len(messages))),
            }
        ]
    if not spans and recorded:
        spans = [
            {
                "match": "unmatched_result",
                "result_index": index,
                "user_message_index": None,
                "message_indexes": [],
            }
            for index in range(len(recorded))
        ]
    links = execution.get("tool_links") if isinstance(execution.get("tool_links"), list) else []
    refs = trace.get("external_references") if isinstance(trace.get("external_references"), list) else []
    call_links, result_links = _index_links(links)
    message_refs, block_refs, input_refs = _index_references(refs)
    events: list[dict[str, Any]] = []
    state = {"user_turn": 0, "seen_user": False}
    consumed: set[int] = set()
    tool_names = _defined_tool_names(config.get("tools"))
    for span in spans:
        match = span.get("match")
        result_index = span.get("result_index") if isinstance(span.get("result_index"), int) else None
        indexes = [index for index in span.get("message_indexes") or [] if isinstance(index, int)]
        if match == "unmatched_result":
            state["user_turn"] += 1
            state["seen_user"] = True
            _emit_unmatched_result(
                events, recorded, result_index, state["user_turn"], block_refs, consumed
            )
            continue
        if match == "paired":
            state["user_turn"] += 1
            state["seen_user"] = True
            _emit_span(
                events,
                messages,
                recorded,
                indexes,
                state["user_turn"],
                "paired",
                result_index,
                span.get("user_message_index"),
                True,
                call_links,
                result_links,
                message_refs,
                block_refs,
                consumed,
                tool_names,
            )
            continue
        for segment in _message_segments(messages, indexes):
            roles = [_role(messages, index) for index in segment]
            has_user = "user" in roles
            if has_user:
                state["user_turn"] += 1
                state["seen_user"] = True
                turn: int | None = state["user_turn"]
            elif not state["seen_user"] and roles and all(role in _SETUP_ROLES for role in roles):
                turn = None
            else:
                turn = state["user_turn"] if state["seen_user"] else None
            _emit_span(
                events,
                messages,
                recorded,
                segment,
                turn,
                "unmatched_messages",
                None,
                segment[0] if has_user else None,
                has_user,
                call_links,
                result_links,
                message_refs,
                block_refs,
                consumed,
                tool_names,
            )
    _flag_buried_input_refs(events, input_refs, consumed)
    return [{"index": index, **event} for index, event in enumerate(events)]


def _emit_span(
    events: list[dict[str, Any]],
    messages: list[Any],
    recorded: list[Any],
    indexes: list[int],
    turn: int | None,
    turn_match: str,
    result_index: int | None,
    user_message_index: Any,
    has_user: bool,
    call_links: dict[tuple[int, int], dict[str, Any]],
    result_links: dict[int, dict[str, Any]],
    message_refs: dict[int, list[tuple[int, dict[str, Any], str]]],
    block_refs: dict[tuple[int, int], list[tuple[int, dict[str, Any]]]],
    consumed: set[int],
    tool_names: set[Any],
) -> None:
    start = len(events)
    response_at = _response_index(messages, indexes)
    for index in indexes:
        message = messages[index] if 0 <= index < len(messages) else None
        if not isinstance(message, dict):
            _append(
                events,
                _event(
                    "message",
                    turn,
                    turn_match,
                    index,
                    result_index,
                    f"result.config.messages[{index}]",
                    empty=True,
                ),
            )
            continue
        role = message.get("role")
        if role in _SETUP_ROLES and turn is None:
            kind = "setup"
        elif role in _SETUP_ROLES:
            kind = "system"
        elif role == "user":
            kind = "user_input"
        elif role == "assistant" and index == response_at:
            kind = "assistant_response"
        elif role == "assistant":
            kind = "assistant_message"
        elif role in _TOOL_ROLES:
            kind = "tool_result"
        else:
            kind = "message"
        if kind == "user_input":
            built = _user_event(
                message,
                index,
                turn,
                turn_match,
                result_index,
                user_message_index if index == user_message_index else None,
                recorded,
                message_refs.get(index, []),
                consumed,
            )
        elif kind == "tool_result":
            built = _tool_result_event(
                message,
                index,
                turn,
                turn_match,
                result_index,
                result_links.get(index),
                message_refs.get(index, []),
                consumed,
            )
        elif kind in {"assistant_message", "assistant_response"}:
            built = _assistant_event(
                message,
                index,
                kind,
                turn,
                turn_match,
                result_index,
                message_refs.get(index, []),
                consumed,
            )
        else:
            built = _text_event(
                message,
                index,
                kind,
                turn,
                turn_match,
                result_index,
                message_refs.get(index, []),
                consumed,
            )
        if turn_match == "unmatched_messages" and (kind in {"setup", "user_input"} or not has_user):
            _add_missing(built, ["result"])
        _append(events, built)
        if role == "assistant":
            for call_index, call in _assistant_calls(message):
                _append(
                    events,
                    _tool_call_event(
                        index,
                        call_index,
                        call,
                        turn,
                        turn_match,
                        result_index,
                        call_links.get((index, call_index)),
                        tool_names,
                    ),
                )
    _attach_output(
        events,
        start,
        messages,
        indexes,
        recorded,
        result_index,
        turn,
        turn_match,
        block_refs,
        consumed,
    )


def _emit_unmatched_result(
    events: list[dict[str, Any]],
    recorded: list[Any],
    result_index: int | None,
    turn: int,
    block_refs: dict[tuple[int, int], list[tuple[int, dict[str, Any]]]],
    consumed: set[int],
) -> None:
    if result_index is None or not 0 <= result_index < len(recorded):
        _append(
            events,
            _event(
                "user_input",
                turn,
                "unmatched_result",
                None,
                result_index,
                "result.results",
                content_match="result_only",
                content_source="result",
                empty=True,
                missing=["messages"],
            ),
        )
        return
    item = recorded[result_index] if isinstance(recorded[result_index], dict) else {}
    user_input = item.get("input") if isinstance(item.get("input"), dict) else {}
    event = _event(
        "user_input",
        turn,
        "unmatched_result",
        None,
        result_index,
        f"result.results[{result_index}].input",
        content_match="result_only",
        content_source="result",
        missing=["messages"],
    )
    if "text" in user_input and user_input.get("text") not in (None, ""):
        event["content"] = copy.deepcopy(user_input.get("text"))
    else:
        event["empty"] = True
    context = {
        key: copy.deepcopy(value) for key, value in user_input.items() if key != "text"
    }
    if context:
        context["source_path"] = f"result.results[{result_index}].input"
        event["input_context"] = context
    _append(events, event)
    _attach_output(
        events,
        len(events),
        [],
        [],
        recorded,
        result_index,
        turn,
        "unmatched_result",
        block_refs,
        consumed,
    )


def _user_event(
    message: dict[str, Any],
    index: int,
    turn: int | None,
    turn_match: str,
    result_index: int | None,
    matched_result: Any,
    recorded: list[Any],
    refs: list[tuple[int, dict[str, Any], str]],
    consumed: set[int],
) -> dict[str, Any]:
    event = _event(
        "user_input",
        turn,
        turn_match,
        index,
        result_index,
        f"result.config.messages[{index}]",
    )
    content = message.get("content") if "content" in message else None
    _fill_content(event, content, refs, "message", consumed)
    unavailable = _unavailable_parts(content)
    if unavailable:
        event["unavailable_parts"] = unavailable
    user_input = _recorded_input(recorded, result_index) if matched_result is not None else None
    if user_input is None:
        event["content_match"] = "message_only"
    else:
        content_match, context = _dedupe_input(content, user_input, result_index)
        event["content_match"] = content_match
        if context:
            event["input_context"] = context
    fields = _message_fields(message)
    if fields:
        event["message_fields"] = fields
    return event


def _assistant_event(
    message: dict[str, Any],
    index: int,
    kind: str,
    turn: int | None,
    turn_match: str,
    result_index: int | None,
    refs: list[tuple[int, dict[str, Any], str]],
    consumed: set[int],
) -> dict[str, Any]:
    event = _event(
        kind, turn, turn_match, index, result_index, f"result.config.messages[{index}]"
    )
    content = message.get("content") if "content" in message else None
    _fill_content(event, content, refs, "message", consumed)
    unavailable = _unavailable_parts(content)
    if unavailable:
        event["unavailable_parts"] = unavailable
    fields = _message_fields(message)
    if fields:
        event["message_fields"] = fields
    return event


def _text_event(
    message: dict[str, Any],
    index: int,
    kind: str,
    turn: int | None,
    turn_match: str,
    result_index: int | None,
    refs: list[tuple[int, dict[str, Any], str]],
    consumed: set[int],
) -> dict[str, Any]:
    event = _event(
        kind, turn, turn_match, index, result_index, f"result.config.messages[{index}]"
    )
    content = message.get("content") if "content" in message else None
    _fill_content(event, content, refs, "message", consumed)
    fields = _message_fields(message)
    if fields:
        event["message_fields"] = fields
    return event


def _tool_result_event(
    message: dict[str, Any],
    index: int,
    turn: int | None,
    turn_match: str,
    result_index: int | None,
    link: dict[str, Any] | None,
    refs: list[tuple[int, dict[str, Any], str]],
    consumed: set[int],
) -> dict[str, Any]:
    event = _event(
        "tool_result",
        turn,
        turn_match,
        index,
        result_index,
        f"result.config.messages[{index}]",
    )
    call_id = message.get("tool_call_id")
    if call_id is None and "name" in message:
        call_id = message.get("name")
    if call_id is not None:
        event["call_id"] = call_id
    if "name" in message:
        event["name"] = message.get("name")
    if link:
        event["link_status"] = link.get("status")
        if link.get("call_message_index") is not None:
            event["call_message_index"] = link["call_message_index"]
        if link.get("status") != "paired":
            _add_missing(event, ["tool_call"])
    else:
        event["link_status"] = "unmatched_result"
        _add_missing(event, ["tool_call"])
    content = message.get("content") if "content" in message else None
    _fill_content(event, content, refs, "message", consumed)
    fields = _message_fields(message)
    if fields:
        event["message_fields"] = fields
    return event


def _tool_call_event(
    message_index: int,
    call_index: int,
    call: Any,
    turn: int | None,
    turn_match: str,
    result_index: int | None,
    link: dict[str, Any] | None,
    tool_names: set[Any],
) -> dict[str, Any]:
    source = f"result.config.messages[{message_index}].tool_calls[{call_index}]"
    event = _event("tool_call", turn, turn_match, message_index, result_index, source)
    event["call_index"] = call_index
    if not isinstance(call, dict):
        event["empty"] = True
        event["defined"] = False
        event["link_status"] = "unmatched_call"
        _add_missing(event, ["tool_result"])
        return event
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = function.get("name")
    if "id" in call:
        event["call_id"] = call.get("id")
    if name is not None:
        event["name"] = name
    if "arguments" in function:
        event["arguments"] = copy.deepcopy(function.get("arguments"))
    if "type" in call:
        event["type"] = call.get("type")
    event["defined"] = bool(name) and name in tool_names
    function_fields = {
        key: copy.deepcopy(value)
        for key, value in function.items()
        if key not in {"name", "arguments"}
    }
    if function_fields:
        event["function_fields"] = function_fields
    call_fields = {
        key: copy.deepcopy(value)
        for key, value in call.items()
        if key not in {"id", "type", "function"}
    }
    if call_fields:
        event["call_fields"] = call_fields
    if link:
        event["link_status"] = link.get("status")
        if link.get("result_message_index") is not None:
            event["result_message_index"] = link["result_message_index"]
        if link.get("status") != "paired":
            _add_missing(event, ["tool_result"])
    else:
        event["link_status"] = "unmatched_call"
        _add_missing(event, ["tool_result"])
    return event


def _attach_output(
    events: list[dict[str, Any]],
    start: int,
    messages: list[Any],
    indexes: list[int],
    recorded: list[Any],
    result_index: int | None,
    turn: int | None,
    turn_match: str,
    block_refs: dict[tuple[int, int], list[tuple[int, dict[str, Any]]]],
    consumed: set[int],
) -> None:
    if result_index is None or not 0 <= result_index < len(recorded):
        return
    item = recorded[result_index]
    if not isinstance(item, dict) or "output" not in item:
        return
    output = item.get("output")
    response = next(
        (event for event in events[start:] if event.get("kind") == "assistant_response"),
        None,
    )
    original = None
    if response is not None and response.get("message_index") is not None:
        original = _content_of(messages, response["message_index"])
    tool_values = [
        (
            _content_of(messages, index),
            f"result.config.messages[{index}].content",
        )
        for index in indexes
        if _role(messages, index) in _TOOL_ROLES
    ]
    if isinstance(output, dict):
        blocks = output.get("blocks") if isinstance(output.get("blocks"), list) else []
        rendered = []
        for block_index, block in enumerate(blocks):
            rendered_block = _render_block(
                block,
                result_index,
                block_index,
                original,
                tool_values,
                block_refs.get((result_index, block_index), []),
                consumed,
            )
            if rendered_block:
                rendered.append(rendered_block)
        fields = {
            key: copy.deepcopy(value) for key, value in output.items() if key != "blocks"
        }
        output_context: dict[str, Any] = {}
        if rendered:
            output_context["blocks"] = rendered
        if fields:
            output_context["fields"] = fields
        if response is not None:
            if output_context:
                response["output_context"] = output_context
            return
        if output_context:
            _emit_result_output(events, turn, turn_match, result_index, output_context)
            return
        if turn_match == "unmatched_result":
            _append(
                events,
                _event(
                    "assistant_response",
                    turn,
                    turn_match,
                    None,
                    result_index,
                    f"result.results[{result_index}].output",
                    content_source="result",
                    empty=True,
                    missing=["messages"] if turn_match == "unmatched_result" else None,
                ),
            )
        return
    if response is not None and response.get("content") == output:
        return
    _append(
        events,
        _event(
            "assistant_response",
            turn,
            turn_match,
            None,
            result_index,
            f"result.results[{result_index}].output",
            content=copy.deepcopy(output),
            content_source="result",
            missing=["messages"] if turn_match == "unmatched_result" else None,
        ),
    )


def _render_block(
    block: Any,
    result_index: int,
    block_index: int,
    original_assistant: Any,
    tool_values: list[tuple[Any, str]],
    refs: list[tuple[int, dict[str, Any]]],
    consumed: set[int],
) -> dict[str, Any] | None:
    path = f"result.results[{result_index}].output.blocks[{block_index}]"
    if not isinstance(block, dict):
        return {"source": path, "value": copy.deepcopy(block)}
    text = block.get("text") if "text" in block else None
    extras = {key: copy.deepcopy(value) for key, value in block.items() if key != "text"}
    if isinstance(text, str) and isinstance(original_assistant, str) and text == original_assistant:
        _consume(consumed, refs)
        if not extras:
            return None
        return {
            "source": path,
            "text_omitted": True,
            "text_match": "same_as_message",
            **extras,
        }
    for tool_text, tool_source in tool_values:
        if isinstance(text, str) and text == tool_text:
            _consume(consumed, refs)
            if not extras:
                return None
            return {
                "source": path,
                "text_omitted": True,
                "text_match": "same_as_tool_message",
                "same_as": tool_source,
                **extras,
            }
    item: dict[str, Any] = {"source": path, **extras}
    if "text" not in block and not extras:
        item["empty"] = True
        return item
    if "text" not in block:
        return item
    display = copy.deepcopy(text)
    if refs and isinstance(text, str):
        annotation = _external_annotation(refs, "result")
        _consume(consumed, refs)
        display = annotation.get("content", display)
        for key, value in annotation.items():
            if key != "content":
                item[key] = value
    if display not in (None, ""):
        item["text"] = display
    else:
        item["empty"] = True
    item["text_match"] = "differs" if original_assistant is not None else "result_only"
    return item


def _emit_result_output(
    events: list[dict[str, Any]],
    turn: int | None,
    turn_match: str,
    result_index: int,
    output_context: dict[str, Any],
) -> None:
    blocks = output_context.get("blocks") or []
    missing = ["messages"] if turn_match == "unmatched_result" else None
    if len(blocks) == 1 and "text" in blocks[0] and not blocks[0].get("text_omitted"):
        block = blocks[0]
        event = _event(
            "assistant_response",
            turn,
            turn_match,
            None,
            result_index,
            block["source"],
            content=block["text"],
            content_source=block.get("content_source") or "result",
            missing=missing,
        )
        for key in ("truncated", "unresolved", "complete", "external"):
            if key in block:
                event[key] = block[key]
        if "missing" in block:
            _add_missing(event, block["missing"])
        extras = {
            key: value
            for key, value in block.items()
            if key
            not in {
                "source",
                "text",
                "text_match",
                "truncated",
                "unresolved",
                "complete",
                "external",
                "missing",
                "content_source",
                "empty",
            }
        }
        context: dict[str, Any] = {}
        if extras:
            context["blocks"] = [
                {"source": block["source"], "text_omitted": True, "text_match": "same_as_content", **extras}
            ]
        if "fields" in output_context:
            context["fields"] = output_context["fields"]
        if context:
            event["output_context"] = context
        _append(events, event)
        return
    _append(
        events,
        _event(
            "assistant_response",
            turn,
            turn_match,
            None,
            result_index,
            f"result.results[{result_index}].output",
            content_source="result",
            output_context=output_context,
            missing=missing,
        ),
    )


def _dedupe_input(
    content: Any, user_input: dict[str, Any], result_index: int
) -> tuple[str, dict[str, Any] | None]:
    has_text = "text" in user_input
    has_urls = bool(user_input.get("urls"))
    stored = _stored_user_text(user_input)
    same = isinstance(content, str) and (
        (has_text and content == user_input.get("text")) or (has_urls and content == stored)
    )
    if has_text and isinstance(content, str) and content == stored:
        same = True
    context = {
        key: copy.deepcopy(value)
        for key, value in user_input.items()
        if not (key == "text" and same)
    }
    if context:
        context["source_path"] = f"result.results[{result_index}].input"
        if same and has_text:
            context["text_omitted"] = True
    if not has_text and not has_urls:
        return "message_only", context or None
    if same:
        return "same", context or None
    return "differs", context or None


def _fill_content(
    event: dict[str, Any],
    content: Any,
    refs: list[tuple[int, dict[str, Any], str]],
    source_label: str,
    consumed: set[int],
) -> None:
    whole = [pair for pair in refs if pair[2] == ""]
    nested = [pair for pair in refs if pair[2] != ""]
    if whole and isinstance(content, str):
        annotation = _external_annotation([(index, ref) for index, ref, _suffix in whole], source_label)
        _apply_annotation(event, annotation)
        _consume_indexed(consumed, whole)
        if "content" not in event:
            event["empty"] = True
        if nested:
            _apply_nested_flags(event, nested, consumed)
        return
    if content not in (None, "", []):
        event["content"] = copy.deepcopy(content)
        event["content_source"] = source_label
    else:
        event["empty"] = True
        event["content_source"] = source_label
    if nested and "content" in event:
        _apply_nested_content(event, nested, source_label, consumed)
    elif nested:
        _apply_nested_flags(event, nested, consumed)


def _apply_nested_content(
    event: dict[str, Any],
    nested: list[tuple[int, dict[str, Any], str]],
    source_label: str,
    consumed: set[int],
) -> None:
    unresolved = False
    externals: list[dict[str, Any]] = []
    for ref_index, ref, suffix in nested:
        annotation = _external_annotation([(ref_index, ref)], source_label)
        if annotation.get("content") is not None:
            _put_suffix(event["content"], suffix, annotation["content"])
        if annotation.get("complete") is not True:
            unresolved = True
        if "external" in annotation:
            externals.append(annotation["external"])
        _consume_indexed(consumed, [(ref_index, ref, suffix)])
    if not externals:
        return
    event["external"] = externals[0] if len(externals) == 1 else externals
    if unresolved:
        event["truncated"] = True
        event["unresolved"] = True
        event["complete"] = False
        _add_missing(event, ["external_content"])
    else:
        event["complete"] = True


def _apply_nested_flags(
    event: dict[str, Any],
    nested: list[tuple[int, dict[str, Any], str]],
    consumed: set[int],
) -> None:
    externals = []
    unresolved = False
    for ref_index, ref, suffix in nested:
        annotation = _external_annotation([(ref_index, ref)], "message")
        if "external" in annotation:
            externals.append(annotation["external"])
        if annotation.get("complete") is not True:
            unresolved = True
        _consume_indexed(consumed, [(ref_index, ref, suffix)])
    if externals:
        event["external"] = externals[0] if len(externals) == 1 else externals
    if unresolved:
        event["truncated"] = True
        event["unresolved"] = True
        event["complete"] = False
        _add_missing(event, ["external_content"])


def _external_annotation(
    pairs: list[tuple[int, dict[str, Any]]], source_label: str
) -> dict[str, Any]:
    chosen_index, chosen = pairs[0]
    for ref_index, ref in pairs:
        if ref.get("complete") is True and isinstance(ref.get("retrieved_text"), str):
            chosen_index, chosen = ref_index, ref
            break
    external: dict[str, Any] = {
        "reference_index": chosen_index,
        "retrieval_status": chosen.get("retrieval_status"),
    }
    if chosen.get("file_url"):
        external["file_url"] = chosen["file_url"]
    if chosen.get("complete") is True and isinstance(chosen.get("retrieved_text"), str):
        return {
            "content": chosen["retrieved_text"],
            "content_source": "retrieved_external",
            "complete": True,
            "external": external,
        }
    annotation: dict[str, Any] = {
        "content_source": source_label,
        "truncated": True,
        "unresolved": True,
        "complete": False,
        "missing": ["external_content"],
        "external": external,
    }
    preview = chosen.get("available_preview")
    if isinstance(preview, str) and preview != "":
        annotation["content"] = preview
    return annotation


def _flag_buried_input_refs(
    events: list[dict[str, Any]],
    input_refs: list[tuple[int, int, str, dict[str, Any]]],
    consumed: set[int],
) -> None:
    for ref_index, result_index, suffix, ref in input_refs:
        if ref_index in consumed:
            continue
        event = next(
            (
                item
                for item in events
                if item.get("kind") == "user_input" and item.get("result_index") == result_index
            ),
            None,
        )
        if event is None:
            continue
        annotation = _external_annotation([(ref_index, ref)], "result")
        context = event.get("input_context")
        if isinstance(context, dict) and suffix and annotation.get("content") is not None:
            _put_suffix(context, suffix, annotation["content"])
        flags = {
            key: value
            for key, value in annotation.items()
            if key not in {"content", "content_source"}
        }
        _apply_annotation(event, flags)
        consumed.add(ref_index)


def _response_index(messages: list[Any], indexes: list[int]) -> int | None:
    last = None
    for index in indexes:
        if _role(messages, index) == "assistant":
            last = index
    if last is None:
        return None
    message = messages[last]
    if isinstance(message, dict) and _assistant_calls(message):
        return None
    return last


def _assistant_calls(message: dict[str, Any]) -> list[tuple[int, Any]]:
    calls = message.get("tool_calls")
    if isinstance(calls, list) and calls:
        return list(enumerate(calls))
    return []


def _message_segments(messages: list[Any], indexes: list[int]) -> list[list[int]]:
    segments: list[list[int]] = []
    current: list[int] = []
    for index in indexes:
        if _role(messages, index) == "user" and current:
            segments.append(current)
            current = [index]
        else:
            current.append(index)
    if current:
        segments.append(current)
    return segments


def _recorded_input(recorded: list[Any], result_index: int | None) -> dict[str, Any] | None:
    if result_index is None or not 0 <= result_index < len(recorded):
        return None
    item = recorded[result_index]
    if not isinstance(item, dict) or not isinstance(item.get("input"), dict):
        return None
    return item["input"]


def _defined_tool_names(tools: Any) -> set[Any]:
    names: set[Any] = set()
    if not isinstance(tools, list):
        return names
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name"):
            names.add(function["name"])
        elif tool.get("name"):
            names.add(tool["name"])
    return names


def _index_links(
    links: list[Any],
) -> tuple[dict[tuple[int, int], dict[str, Any]], dict[int, dict[str, Any]]]:
    by_call: dict[tuple[int, int], dict[str, Any]] = {}
    by_result: dict[int, dict[str, Any]] = {}
    for link in links:
        if not isinstance(link, dict):
            continue
        call_message = link.get("call_message_index")
        call_index = link.get("call_index")
        if isinstance(call_message, int) and isinstance(call_index, int):
            by_call[(call_message, call_index)] = link
        result_message = link.get("result_message_index")
        if isinstance(result_message, int):
            by_result[result_message] = link
    return by_call, by_result


def _index_references(
    refs: list[Any],
) -> tuple[
    dict[int, list[tuple[int, dict[str, Any], str]]],
    dict[tuple[int, int], list[tuple[int, dict[str, Any]]]],
    list[tuple[int, int, str, dict[str, Any]]],
]:
    messages: dict[int, list[tuple[int, dict[str, Any], str]]] = {}
    blocks: dict[tuple[int, int], list[tuple[int, dict[str, Any]]]] = {}
    inputs: list[tuple[int, int, str, dict[str, Any]]] = []
    for ref_index, ref in enumerate(refs):
        if not isinstance(ref, dict):
            continue
        location = ref.get("location") or ""
        message_match = _MESSAGE_LOCATION.match(location)
        if message_match:
            messages.setdefault(int(message_match.group(1)), []).append(
                (ref_index, ref, message_match.group(2))
            )
            continue
        block_match = _BLOCK_LOCATION.match(location)
        if block_match:
            key = (int(block_match.group(1)), int(block_match.group(2)))
            blocks.setdefault(key, []).append((ref_index, ref))
            continue
        input_match = _INPUT_LOCATION.match(location)
        if input_match:
            inputs.append((ref_index, int(input_match.group(1)), input_match.group(2), ref))
    return messages, blocks, inputs


def _unavailable_parts(content: Any) -> list[str]:
    if not isinstance(content, list):
        return []
    found: list[str] = []
    for index, part in enumerate(content):
        if isinstance(part, str):
            continue
        if not isinstance(part, dict):
            found.append(f"content[{index}]")
            continue
        part_type = part.get("type")
        if part_type == "text":
            if "text" not in part:
                found.append(f"content[{index}].text")
            continue
        if part_type == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image if isinstance(image, str) else None
            if not url:
                found.append(f"content[{index}].image_url")
            continue
        if not _payload_present(part):
            found.append(f"content[{index}]")
    return found


def _payload_present(part: dict[str, Any]) -> bool:
    for key in _PAYLOAD_KEYS:
        value = part.get(key)
        if value not in (None, "", {}, []):
            return True
    return False


def _message_fields(message: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in message.items()
        if key not in _MESSAGE_RESERVED
    }


def _content_of(messages: list[Any], index: Any) -> Any:
    if not isinstance(index, int) or not 0 <= index < len(messages):
        return None
    message = messages[index]
    if isinstance(message, dict):
        return message.get("content")
    return None


def _role(messages: list[Any], index: int) -> Any:
    if not 0 <= index < len(messages) or not isinstance(messages[index], dict):
        return None
    return messages[index].get("role")


def _event(kind: str, turn: int | None, turn_match: str, message_index: int | None, result_index: int | None, source: str, **fields: Any) -> dict[str, Any]:
    event: dict[str, Any] = {"kind": kind, "turn_match": turn_match, "source": source}
    if turn is not None:
        event["turn"] = turn
    if message_index is not None:
        event["message_index"] = message_index
    if result_index is not None:
        event["result_index"] = result_index
    event.update(fields)
    return event


def _append(events: list[dict[str, Any]], event: dict[str, Any]) -> dict[str, Any]:
    cleaned = {
        key: value
        for key, value in event.items()
        if value is not None and value != [] and value != {}
    }
    events.append(cleaned)
    return cleaned


def _add_missing(event: dict[str, Any], items: list[str]) -> None:
    current = list(event.get("missing") or [])
    for item in items:
        if item not in current:
            current.append(item)
    if current:
        event["missing"] = current


def _apply_annotation(event: dict[str, Any], annotation: dict[str, Any]) -> None:
    missing = annotation.get("missing")
    for key, value in annotation.items():
        if key == "missing" or value is None:
            continue
        event[key] = value
    if missing:
        _add_missing(event, list(missing))


def _consume(consumed: set[int], refs: list[tuple[int, dict[str, Any]]]) -> None:
    for ref_index, _ref in refs:
        consumed.add(ref_index)


def _consume_indexed(consumed: set[int], refs: list[tuple[int, dict[str, Any], str]]) -> None:
    for ref_index, _ref, _suffix in refs:
        consumed.add(ref_index)


def _put_suffix(root: Any, suffix: str, value: Any) -> None:
    tokens: list[Any] = []
    for index, name in _PATH_TOKEN.findall(suffix):
        tokens.append(int(index) if index else name)
    if not tokens:
        return
    current = root
    try:
        for token in tokens[:-1]:
            current = current[token]
        current[tokens[-1]] = value
    except (KeyError, IndexError, TypeError):
        return
