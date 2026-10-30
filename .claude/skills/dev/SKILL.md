---
name: dev
description: Start the development server with hot-reload on port 8000.
user-invocable: true
disable-model-invocation: true
allowed-tools: "Bash(uv run uvicorn *) Bash(make dev)"
---

# Start Dev Server

Start the FastAPI development server:

```bash
make dev
```

This runs uvicorn with hot-reload on port 8000 with 3 workers.
