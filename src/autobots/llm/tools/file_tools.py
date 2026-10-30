import re
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field, model_validator

from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.terminal.terminal_connection_manager import terminal_manager
from src.autobots.data_model.app_state import AppState

FileSystemOperation = Literal[
    "write_file",
    "append_file",
    "replace_in_file",
    "stat_file",
    "list_dir",
    "mkdir",
    "move_path",
    "copy_path",
    "delete_path",
    "apply_unified_diff",
    "replace_line_range",
    "insert_at_line",
    "search_in_file",
    "search_in_files",
    "batch",
    "file_write_start",
    "file_write_chunk",
    "file_write_finish",
    "file_write_abort",
    "read_file",
    "get_symbols",
    "get_references",
    "get_outline",
    "get_import_graph",
    "get_call_graph",
    "get_test_coverage",
    "get_type_hierarchy",
    "find_similar",
]


class FileSystemToolConfig(BaseModel):
    operation: FileSystemOperation = Field(
        ...,
        description=(
            "The file system operation to perform. Supported values: "
            "write_file, append_file, replace_in_file, stat_file, list_dir, mkdir, "
            "move_path, copy_path, delete_path, apply_unified_diff, replace_line_range, "
            "insert_at_line, search_in_file, search_in_files, batch, file_write_start, file_write_chunk, "
            "file_write_finish, file_write_abort, read_file, "
            "get_symbols, get_references, get_outline, get_import_graph, "
            "get_call_graph, get_test_coverage, get_type_hierarchy, find_similar."
        ),
    )
    payload: dict[str, Any] = Field(
        ...,
        description="Operation-specific arguments."
    )

    @model_validator(mode="after")
    def validate_payload(self):
        op = self.operation
        payload = self.payload or {}

        required_fields: dict[str, list[str]] = {
            "write_file": ["path", "content"],
            "append_file": ["path", "content"],
            "replace_in_file": ["path", "old_text", "new_text"],
            "stat_file": ["path"],
            "list_dir": ["path"],
            "mkdir": ["path"],
            "move_path": ["src_path", "dst_path"],
            "copy_path": ["src_path", "dst_path"],
            "delete_path": ["path"],
            "apply_unified_diff": ["path", "diff_text"],
            "replace_line_range": ["path", "start_line", "end_line", "new_lines"],
            "insert_at_line": ["path", "line_number", "lines"],
            "search_in_file": ["path", "query"],
            "search_in_files": ["root_path", "query"],
            "batch": ["ops"],
            "file_write_start": ["path"],
            "file_write_chunk": ["upload_id", "chunk"],
            "file_write_finish": ["upload_id"],
            "file_write_abort": ["upload_id"],
            "read_file": ["path", "mode"],
            "get_symbols": ["path"],
            "get_references": ["symbol", "root_path", "definition_file", "definition_line"],
            "get_outline": ["root_path"],
            "get_import_graph": ["root_path"],
            "get_call_graph": ["symbol", "file_path", "definition_line"],
            "get_test_coverage": ["source_file", "root_path"],
            "get_type_hierarchy": ["root_path"],
            "find_similar": ["symbol", "file_path", "definition_line", "root_path"],
        }

        missing = [k for k in required_fields[op] if k not in payload]
        if missing:
            raise ValueError(
                f"Missing required payload fields for operation '{op}': {missing}"
            )

        # Per-mode validation for read_file (each mode has different required + value constraints)
        if op == "read_file":
            _validate_read_file_payload(payload)

        if op == "batch":
            ops = payload.get("ops")
            if not isinstance(ops, list):
                raise ValueError("batch requires payload['ops'] to be a list")

        return self


class FileSystemToolOutput(BaseModel):
    success: bool
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


_READ_FILE_MODES = ("full", "range", "head", "tail", "chunk", "grep")


def _validate_read_file_payload(payload: dict[str, Any]) -> None:
    mode = payload.get("mode")
    if mode not in _READ_FILE_MODES:
        raise ValueError(
            f"Invalid mode for read_file: {mode!r}. "
            f"Must be one of: {', '.join(_READ_FILE_MODES)}"
        )

    if mode == "range":
        start = payload.get("start_line")
        end = payload.get("end_line")
        # At least one of start_line / end_line should be provided.
        if start is None and end is None:
            raise ValueError(
                "read_file mode='range' requires at least one of 'start_line' or 'end_line'"
            )
        if start is not None and (not isinstance(start, int) or start < 1):
            raise ValueError("read_file mode='range': 'start_line' must be a positive integer")
        if end is not None and (not isinstance(end, int) or end < 1):
            raise ValueError("read_file mode='range': 'end_line' must be a positive integer")
        if (
            start is not None
            and end is not None
            and isinstance(start, int)
            and isinstance(end, int)
            and end < start
        ):
            raise ValueError("read_file mode='range': 'end_line' must be >= 'start_line'")

    elif mode in ("head", "tail"):
        num_lines = payload.get("num_lines")
        if not isinstance(num_lines, int) or num_lines < 1:
            raise ValueError(
                f"read_file mode={mode!r}: 'num_lines' must be a positive integer"
            )

    elif mode == "chunk":
        start_byte = payload.get("start_byte")
        max_bytes = payload.get("max_bytes")
        if not isinstance(start_byte, int) or start_byte < 0:
            raise ValueError("read_file mode='chunk': 'start_byte' must be a non-negative integer")
        if not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("read_file mode='chunk': 'max_bytes' must be a positive integer")

    elif mode == "grep":
        pattern = payload.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("read_file mode='grep': 'pattern' must be a non-empty string")
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError(
                f"read_file mode='grep': 'pattern' is not a valid regex: {e}"
            )

    max_bytes_limit = payload.get("max_bytes_limit")
    if max_bytes_limit is not None and (
        not isinstance(max_bytes_limit, int) or max_bytes_limit < 1
    ):
        raise ValueError("'max_bytes_limit' must be a positive integer when provided")

    max_lines_limit = payload.get("max_lines_limit")
    if max_lines_limit is not None and (
        not isinstance(max_lines_limit, int) or max_lines_limit < 1
    ):
        raise ValueError("'max_lines_limit' must be a positive integer when provided")

    line_numbers = payload.get("line_numbers")
    if line_numbers is not None and not isinstance(line_numbers, bool):
        raise ValueError("'line_numbers' must be a boolean when provided")

    max_chars = payload.get("max_chars")
    if max_chars is not None and (not isinstance(max_chars, int) or max_chars < 1):
        raise ValueError(
            "'max_chars' must be a positive integer (>= 1) when provided. "
            "Omit the field for unlimited reads."
        )


async def file_system_tool(
    config: FileSystemToolConfig,
    ctx: Context,
    app_state: AppState,
    notify: Notify | None = None,
) -> FileSystemToolOutput | Exception:
    """
    Unified file system tool.

    IMPORTANT:
    - This is the PRIMARY tool for file and directory manipulation.
    - Do NOT use run_terminal_command for editing/writing files when this tool can do it.
    - Prefer precise edits over full rewrites.
    - For very large file content, use file_write_start/file_write_chunk/file_write_finish.

    INPUT FORMAT:
    {
        "operation": "replace_in_file",
        "payload": {
            "path": "src/app.py",
            "old_text": "DEBUG = True",
            "new_text": "DEBUG = False",
            "replace_all": false
        }
    }

    COMMON OPERATIONS:

    1. write_file
    payload:
    {
        "path": "src/app.py",
        "content": "print('hello')\\n",
        "encoding": "utf-8",
        "mode": "overwrite",
        "make_parents": true
    }

    2. append_file
    payload:
    {
        "path": "notes.txt",
        "content": "\\nnew note\\n",
        "encoding": "utf-8",
        "create_if_missing": true
    }

    3. replace_in_file
    payload:
    {
        "path": "src/app.py",
        "old_text": "foo",
        "new_text": "bar",
        "replace_all": false,
        "encoding": "utf-8",
        "expected_replacements": 1
    }

    4. stat_file
    payload:
    {
        "path": "src/app.py"
    }

    5. list_dir
    payload:
    {
        "path": "src",
        "recursive": false,
        "max_entries": 200,
        "include_hidden": false
    }

    6. mkdir
    payload:
    {
        "path": "src/utils",
        "parents": true,
        "exist_ok": true
    }

    7. move_path
    payload:
    {
        "src_path": "old.py",
        "dst_path": "new.py",
        "overwrite": false,
        "make_parents": true
    }

    8. copy_path
    payload:
    {
        "src_path": "src/template.py",
        "dst_path": "src/new_module.py",
        "overwrite": false,
        "make_parents": true,
        "recursive": true,
        "follow_symlinks": false
    }

    9. delete_path
    payload:
    {
        "path": "tmp/output.txt",
        "recursive": false,
        "missing_ok": true
    }

    10. apply_unified_diff
    payload:
    {
        "path": "src/app.py",
        "diff_text": "@@ -1,2 +1,3 @@\\n import os\\n+import json\\n"
    }

    11. replace_line_range
    payload:
    {
        "path": "src/app.py",
        "start_line": 10,
        "end_line": 15,
        "new_lines": ["def x():", "    return 1"],
        "encoding": "utf-8"
    }

    12. insert_at_line
    payload:
    {
        "path": "src/app.py",
        "line_number": 3,
        "lines": ["import json", "from pathlib import Path"],
        "encoding": "utf-8"
    }

    13. search_in_file
    payload:
    {
        "path": "src/app.py",
        "query": "def process_data",
        "is_regex": false,
        "case_sensitive": false,
        "max_matches": 20,
        "context_lines": 2,
        "encoding": "utf-8"
    }

    14. search_in_files  (repo grep)
    payload:
    {
        "root_path": "src",
        "query": "run_terminal_command",
        "is_regex": false,
        "case_sensitive": false,
        "include_globs": ["*.py", "*.ts", "*.md"],
        "exclude_globs": ["*/generated/*"],
        "include_hidden": false,
        "exclude_dir_names": [".git", ".venv", "build", "dist", "node_modules"],
        "max_files": 2000,
        "max_matches": 200,
        "max_matches_per_file": 50,
        "context_lines": 2,
        "max_file_size_bytes": 2000000,
        "max_total_bytes": 50000000,
        "max_pruned_dirs": 200,
        "encoding": "utf-8"
    }

    15. batch  (sequential multi-op)
    payload:
    {
        "ops": [
            {"operation": "mkdir", "payload": {"path": "src/new", "parents": true, "exist_ok": true}},
            {"operation": "write_file", "payload": {"path": "src/new/a.py", "content": "print('hi')\\n"}},
            {"operation": "replace_in_file", "payload": {"path": "src/app.py", "old_text": "foo", "new_text": "bar"}}
        ],
        "stop_on_error": true,
        "allow_destructive": false,
        "max_ops": 50
    }

    16. file_write_start
    payload:
    {
        "path": "src/generated.py",
        "encoding": "utf-8",
        "mode": "overwrite",
        "make_parents": true
    }

    17. file_write_chunk
    payload:
    {
        "upload_id": "<upload_id>",
        "chunk": "next content chunk..."
    }

    18. file_write_finish
    payload:
    {
        "upload_id": "<upload_id>"
    }

    19. file_write_abort
    payload:
    {
        "upload_id": "<upload_id>"
    }

    20. read_file  (modes: full | range | head | tail | chunk | grep)
    Optional limits: max_chars, max_bytes_limit, max_lines_limit, line_numbers.
    payload (full):
    {
        "path": "src/app.py",
        "mode": "full",
        "max_chars": 200000,
        "line_numbers": true
    }
    payload (range):
    {
        "path": "src/app.py",
        "mode": "range",
        "start_line": 100,
        "end_line": 200
    }
    payload (head/tail):
    {
        "path": "build.log",
        "mode": "head",
        "num_lines": 50
    }
    payload (chunk):
    {
        "path": "data/big.log",
        "mode": "chunk",
        "start_byte": 4096,
        "max_bytes": 8192
    }
    payload (grep):
    {
        "path": "src/app.py",
        "mode": "grep",
        "pattern": "def process_",
        "context_lines_before": 2,
        "context_lines_after": 5,
        "max_matches": 50
    }

    BEST PRACTICES:
    - For existing files: use search_in_file first, then a targeted edit.
    - Prefer replace_in_file / replace_line_range / insert_at_line over full rewrite.
    - Use write_file for new or fully rewritten small/medium files.
    - Use chunked write operations for very large content.
    - After important edits, verify by searching or stat'ing if needed.

    CODE INTELLIGENCE OPERATIONS (via codeglass):

    21. get_symbols — extract all symbols (functions, classes, methods) from a file
    payload:
    {
        "path": "src/app.py"
    }

    22. get_references — find all usages of a symbol across the codebase
    payload:
    {
        "symbol": "process_data",
        "root_path": "src",
        "definition_file": "src/lib.py",
        "definition_line": 42
    }

    23. get_outline — directory tree with symbol counts per file
    payload:
    {
        "root_path": "src",
        "max_depth": 3
    }

    24. get_import_graph — who imports what, who depends on this file
    payload:
    {
        "root_path": "src"
    }

    25. get_call_graph — what does this function call?
    payload:
    {
        "symbol": "handle_request",
        "file_path": "src/server.py",
        "definition_line": 100
    }

    26. get_test_coverage — find test files and covered/uncovered symbols
    payload:
    {
        "source_file": "src/lib.py",
        "root_path": "."
    }

    27. get_type_hierarchy — class inheritance graph
    payload:
    {
        "root_path": "src"
    }

    28. find_similar — find functions similar to a given one
    payload:
    {
        "symbol": "process_data",
        "file_path": "src/lib.py",
        "definition_line": 42,
        "root_path": "src",
        "max_results": 10
    }
    """
    try:
        try:
            agent_id = await terminal_manager.resolve_agent_for_consumer(ctx)
        except ValueError as e:
            return FileSystemToolOutput(
                success=False,
                message=str(e),
                data={},
            )

        if not agent_id:
            return FileSystemToolOutput(
                success=False,
                message=(
                    "No unpaired terminal CLI agent connected for this chat. "
                    "Please run the CLI agent on your machine first, or release an existing pairing."
                ),
                data={},
            )

        request = {
            "type": config.operation,
            **config.payload,
        }

        timeout_map = {
            "stat_file": 30,
            "list_dir": 60,
            "mkdir": 30,
            "move_path": 60,
            "copy_path": 180,
            "delete_path": 60,
            "search_in_file": 60,
            "search_in_files": 180,
            "batch": 600,
            "replace_in_file": 120,
            "replace_line_range": 120,
            "insert_at_line": 120,
            "apply_unified_diff": 180,
            "write_file": 180,
            "append_file": 180,
            "file_write_start": 30,
            "file_write_chunk": 60,
            "file_write_finish": 180,
            "file_write_abort": 30,
            "read_file": 60,
            "get_symbols": 240,
            "get_references": 240,
            "get_outline": 240,
            "get_import_graph": 300,
            "get_call_graph": 240,
            "get_test_coverage": 240,
            "get_type_hierarchy": 240,
            "find_similar": 240,
        }
        timeout_sec = timeout_map.get(config.operation, 120)

        res = await terminal_manager.send_ws_request_to_agent(
            ctx=ctx,
            agent_id=agent_id,
            message=request,
            timeout_sec=timeout_sec,
        )

        if isinstance(res, Exception):
            return res

        if res.get("success") is False and res.get("message"):
            return FileSystemToolOutput(
                success=False,
                message=str(res.get("message", "")),
                data={k: v for k, v in res.items() if k not in {"type", "success", "message"}},
            )

        success = bool(res.get("success", False))
        message = str(res.get("message", ""))

        data = dict(res)
        data.pop("type", None)
        data.pop("success", None)
        data.pop("message", None)

        return FileSystemToolOutput(
            success=success,
            message=message,
            data=data,
        )

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in file_system_tool: {e}")
        return e
