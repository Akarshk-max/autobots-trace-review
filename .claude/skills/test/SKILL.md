---
name: test
description: Run tests. Use /test to run all tests, /test path/to/file.py for a specific file, /test -k pattern for matching tests.
user-invocable: true
allowed-tools: "Bash(uv run pytest *) Bash(python -m pytest *)"
---

# Run Tests

Run the test suite with appropriate options.

If arguments provided: `uv run pytest $ARGUMENTS -vv --timeout=300`
If no arguments: `uv run pytest -n 15 --timeout=300`

Report the results clearly: total passed, failed, errors, and any failure details.
