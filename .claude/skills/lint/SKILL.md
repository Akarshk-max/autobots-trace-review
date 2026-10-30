---
name: lint
description: Run ruff linter and isort on the codebase or specific files. Use /lint for all files, /lint path/to/file.py for a specific file.
user-invocable: true
allowed-tools: "Bash(uv run ruff *) Bash(uv run isort *)"
---

# Lint & Format

If arguments provided, lint specific files:
```bash
uv run ruff check --fix $ARGUMENTS
uv run ruff format $ARGUMENTS
uv run isort $ARGUMENTS
```

If no arguments, lint the entire project:
```bash
uv run ruff check --fix .
uv run ruff format .
uv run isort .
```

Report any remaining issues that couldn't be auto-fixed.
