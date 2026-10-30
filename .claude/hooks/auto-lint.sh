#!/bin/bash
# Auto-lint Python files after edits
# Runs ruff on the edited file to catch issues early

FILE="${TOOL_INPUT_FILE_PATH:-}"

# Only lint Python files
if [[ "$FILE" == *.py ]]; then
    cd "$CLAUDE_PROJECT_DIR" || exit 0
    uv run ruff check --fix "$FILE" 2>/dev/null || true
    uv run ruff format "$FILE" 2>/dev/null || true
fi

exit 0
