from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Union

JsonKey = Union[str, int]


def _truncate_text(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    # keep output length bounded by `limit` including ellipsis
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _one_line(text: str) -> str:
    # Keep notify payload readable as a list view (no giant multi-line values)
    return (
        text.replace("\\", "\\\\")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


@dataclass
class _Node:
    kind: Literal["object", "array", "scalar"]
    # for scalar
    value: Optional[str] = None
    complete: bool = False

    # for object / array
    children: Dict[JsonKey, "_Node"] = field(default_factory=dict)
    order: List[JsonKey] = field(default_factory=list)

    def child(self, key: JsonKey, kind: Literal["object", "array", "scalar"]) -> "_Node":
        if key in self.children:
            return self.children[key]
        node = _Node(kind=kind)
        self.children[key] = node
        self.order.append(key)
        return node


@dataclass
class _Ctx:
    kind: Literal["object", "array"]
    node: _Node
    pending_key: Optional[str] = None
    next_index: int = 0
    emitted_overflow_notice: bool = False


class ToolArgsStreamer:
    """Incrementally parses a JSON *string* (the tool-call `arguments`) and
    produces a human-readable, structured view suitable for streaming.

    Design goals:
    - Never emit raw JSON brackets/quotes during streaming.
    - Emit a field as soon as its value is complete OR reaches `value_char_limit`.
    - Truncate *everything* we render (keys, scalar values).
    - Keep values single-line.

    This is a best-effort incremental parser; it is robust enough for standard
    tool-argument objects (nested dicts/lists + primitives).
    """

    def __init__(
        self,
        tool_name: str,
        *,
        key_char_limit: int = 100,
        value_char_limit: int = 100,
        max_children_per_container: int = 40,
        max_total_lines: int = 120,
        max_render_chars: int = 4500,
    ) -> None:
        self.tool_name = tool_name
        self.key_char_limit = key_char_limit
        self.value_char_limit = value_char_limit
        self.max_children_per_container = max_children_per_container
        self.max_total_lines = max_total_lines
        self.max_render_chars = max_render_chars

        self.root = _Node(kind="object")
        self._stack: List[_Ctx] = []

        # parser state
        self._in_string: bool = False
        self._string_is_key: bool = False
        self._escape: bool = False
        self._buf: List[str] = []

        self._in_literal: bool = False
        self._literal_buf: List[str] = []

        self._skipping_string: bool = False  # after hitting value limit
        self._skipping_literal: bool = False  # after hitting value limit
        self._current_scalar_node: Optional[_Node] = None

        self._dirty: bool = True
        self._started: bool = False
    def has_updates(self) -> bool:
        return self._dirty

    def mark_clean(self) -> None:
        self._dirty = False

    def ingest(self, chunk: str) -> None:
        if not chunk:
            return
        for ch in chunk:
            self._feed_char(ch)

    def finalize_from_full(self, arguments_json: str) -> None:
        """Optional: re-run parser from scratch on the final JSON string.

        This can fill any gaps if streaming deltas were lost or if we skipped
        emitting some values due to line caps.
        """
        # Reset and parse once.
        self.__init__(
            tool_name=self.tool_name,
            key_char_limit=self.key_char_limit,
            value_char_limit=self.value_char_limit,
            max_children_per_container=self.max_children_per_container,
            max_total_lines=self.max_total_lines,
            max_render_chars=self.max_render_chars,
        )
        self.ingest(arguments_json)

    def render(self) -> str:
        lines: List[str] = [f"Calling tool: {self.tool_name}", "Arguments:"]
        self._render_node(self.root, lines, indent=0, label=None)
        # Keep UI from looking blank right after the tool starts.
        if len(lines) == 2:
            lines.append("- (streaming arguments...)")
        if len(lines) > self.max_total_lines:
            lines = lines[: self.max_total_lines]
            lines.append("... (arguments output truncated)")

        # Hard cap render size so Notify() doesn't chop off the beginning of the message
        # (Notify truncates to the *last* 5000 chars, which would hide the useful header).
        capped: List[str] = []
        total = 0
        for line in lines:
            add = len(line) + 1  # include newline
            if total + add > self.max_render_chars:
                if not capped or capped[-1] != "... (arguments output truncated)":
                    capped.append("... (arguments output truncated)")
                break
            capped.append(line)
            total += add
        return "\n".join(capped)

    # -----------------
    # Internal rendering
    # -----------------

    def _render_node(
        self,
        node: _Node,
        out: List[str],
        *,
        indent: int,
        label: Optional[str],
    ) -> None:
        prefix = "  " * indent + "- "

        if label is not None:
            label_disp = _truncate_text(_one_line(str(label)), self.key_char_limit)

            if node.kind == "scalar":
                if node.complete and node.value is not None:
                    out.append(f"{prefix}{label_disp}: {node.value}")
                # don't render incomplete scalars
                return

            # containers
            out.append(f"{prefix}{label_disp}:")
            indent += 1

        # render children (object/array)
        if node.kind in ("object", "array"):
            for k in node.order[: self.max_children_per_container]:
                child = node.children[k]
                if node.kind == "array":
                    child_label = f"[{k}]"
                else:
                    child_label = str(k)
                self._render_node(child, out, indent=indent, label=child_label)

            if len(node.order) > self.max_children_per_container:
                out.append("  " * indent + "- ... (more fields omitted)")

    # -----------------
    # Internal parsing
    # -----------------

    def _current_container(self) -> _Node:
        if self._stack:
            return self._stack[-1].node
        return self.root

    def _current_ctx(self) -> Optional[_Ctx]:
        return self._stack[-1] if self._stack else None

    def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        # We expect a JSON object at the root; but even if the first token
        # is '{', our logic handles it.

    def _push_container(self, kind: Literal["object", "array"], node: _Node) -> None:
        self._stack.append(_Ctx(kind=kind, node=node))

    def _pop_container_if_any(self, kind: Literal["object", "array"]) -> None:
        if self._stack and self._stack[-1].kind == kind:
            self._stack.pop()

    def _alloc_value_node(self, value_kind: Literal["object", "array", "scalar"]) -> _Node:
        ctx = self._current_ctx()
        container = self._current_container()

        if ctx is None:
            # root scalar is unusual; just attach to root pseudo key
            return container.child("value", value_kind)

        if ctx.kind == "object":
            key = ctx.pending_key or "<unknown>"
            # cap children
            if len(container.order) >= self.max_children_per_container and key not in container.children:
                if not ctx.emitted_overflow_notice:
                    ctx.emitted_overflow_notice = True
                    self._dirty = True
                # still create node, but keep it under a synthetic key to avoid explosion
                key = "<omitted>"
            node = container.child(key, value_kind)
            ctx.pending_key = None
            return node

        # array
        idx = ctx.next_index
        ctx.next_index += 1
        if len(container.order) >= self.max_children_per_container and idx not in container.children:
            if not ctx.emitted_overflow_notice:
                ctx.emitted_overflow_notice = True
                self._dirty = True
            idx = self.max_children_per_container  # all overflow coalesces
        return container.child(idx, value_kind)

    def _feed_char(self, ch: str) -> None:
        self._ensure_started()

        # If we're skipping a long string value, just look for its end.
        if self._skipping_string:
            if self._escape:
                self._escape = False
                return
            if ch == "\\":
                self._escape = True
                return
            if ch == '"':
                # end of skipped string
                self._skipping_string = False
                self._in_string = False
                self._current_scalar_node = None
            return

        # handle ongoing string
        if self._in_string:
            if self._escape:
                self._buf.append(ch)
                self._escape = False
                return
            if ch == "\\":
                self._buf.append(ch)
                self._escape = True
                return
            if ch == '"':
                s = "".join(self._buf)
                self._buf = []
                self._in_string = False

                if self._string_is_key:
                    key = _truncate_text(_one_line(s), self.key_char_limit)
                    ctx = self._current_ctx()
                    if ctx is not None and ctx.kind == "object":
                        ctx.pending_key = key
                    # wait for ':'
                    return

                # value string complete
                if self._current_scalar_node is None:
                    self._current_scalar_node = self._alloc_value_node("scalar")
                val = _truncate_text(_one_line(s), self.value_char_limit)
                self._current_scalar_node.kind = "scalar"
                self._current_scalar_node.value = val
                self._current_scalar_node.complete = True
                self._current_scalar_node = None
                self._dirty = True
                return

            # normal char inside string
            self._buf.append(ch)
            if not self._string_is_key and len(self._buf) >= self.value_char_limit:
                # emit truncated value immediately and skip rest until string end
                if self._current_scalar_node is None:
                    self._current_scalar_node = self._alloc_value_node("scalar")
                partial = "".join(self._buf)
                val = _truncate_text(_one_line(partial), self.value_char_limit)
                self._current_scalar_node.kind = "scalar"
                self._current_scalar_node.value = val
                self._current_scalar_node.complete = True
                self._dirty = True
                self._buf = []
                self._skipping_string = True
            return

        # handle ongoing literal/number
        if self._in_literal:
            if self._skipping_literal:
                # We've already emitted a truncated literal; consume until delimiter.
                if ch in ",}] \t\r\n":
                    self._literal_buf = []
                    self._in_literal = False
                    self._skipping_literal = False
                    self._current_scalar_node = None

                    # re-process delimiter
                    self._feed_char(ch)
                return

            if ch in ",}] \t\r\n":
                lit = "".join(self._literal_buf)
                self._literal_buf = []
                self._in_literal = False

                if self._current_scalar_node is None:
                    self._current_scalar_node = self._alloc_value_node("scalar")
                val = _truncate_text(_one_line(lit), self.value_char_limit)
                self._current_scalar_node.kind = "scalar"
                self._current_scalar_node.value = val
                self._current_scalar_node.complete = True
                self._current_scalar_node = None
                self._dirty = True

                # re-process delimiter
                self._feed_char(ch)
                return
            else:
                self._literal_buf.append(ch)
                if len(self._literal_buf) >= self.value_char_limit:
                    # emit and then keep consuming until delimiter, but don't keep updating
                    lit = "".join(self._literal_buf)
                    if self._current_scalar_node is None:
                        self._current_scalar_node = self._alloc_value_node("scalar")
                    val = _truncate_text(_one_line(lit), self.value_char_limit)
                    self._current_scalar_node.kind = "scalar"
                    self._current_scalar_node.value = val
                    self._current_scalar_node.complete = True
                    self._dirty = True
                    # stop growing buffer
                    self._literal_buf = self._literal_buf[: self.value_char_limit]
                    self._skipping_literal = True
                return

        # whitespace
        if ch in " \t\r\n":
            return

        # structure tokens
        if ch == '{':
            node = self._alloc_value_node("object") if self._stack else self.root
            node.kind = "object"
            self._push_container("object", node)
            self._dirty = True
            return

        if ch == '[':
            node = self._alloc_value_node("array") if self._stack else self.root
            node.kind = "array"
            self._push_container("array", node)
            self._dirty = True
            return

        if ch == '}':
            self._pop_container_if_any("object")
            self._dirty = True
            return

        if ch == ']':
            self._pop_container_if_any("array")
            self._dirty = True
            return

        if ch == ',':
            # after value, next key/element
            return

        if ch == ':':
            # value expected next
            return

        if ch == '"':
            # determine whether this is key or value string
            ctx = self._current_ctx()
            self._string_is_key = bool(ctx and ctx.kind == "object" and ctx.pending_key is None)
            self._in_string = True
            self._escape = False
            self._buf = []
            self._current_scalar_node = None
            return

        # start literal/number
        if ch in "-0123456789tfn":
            self._in_literal = True
            self._literal_buf = [ch]
            self._current_scalar_node = None
            return

        # unknown token: ignore (best-effort)
        return
