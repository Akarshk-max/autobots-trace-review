---
paths:
  - "tests/**/*.py"
---

# Testing Rules

- Use `@pytest.mark.asyncio` for async tests
- Use fixtures from `conftest.py`: `ctx`, `user`, `db`, `motor_client`, `test_client`
- Tests must be safe for parallel execution (pytest-xdist, up to 15 workers)
- Never call real external APIs (OpenAI, Claude, etc.) in tests - mock them
- MongoDB fixtures are function-scoped (fresh connection per test)
- Timeout is 300s per test; keep tests fast
- Run single test: `uv run pytest tests/path/test_file.py::test_name -vv`
