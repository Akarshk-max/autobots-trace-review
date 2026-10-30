---
name: new-connector
description: Scaffold a new external service connector in the conn/ module. Usage /new-connector connector_name
user-invocable: true
argument-hint: "[connector_name]"
allowed-tools: "Read Glob Grep Edit Write"
---

# Create New Connector

Scaffold a new external service connector named `$ARGUMENTS`.

## Steps

1. Look at an existing connector in `src/autobots/conn/` for reference (e.g., `openai/` or `claude/`)
2. Create directory: `src/autobots/conn/$ARGUMENTS/`
3. Create files:
   - `{name}_client.py` - Async client wrapper for the external service
   - `{name}_models.py` - Pydantic request/response models
   - `__init__.py` - Public exports
4. Add any required settings to `src/autobots/core/settings.py`
5. Add API key / config vars to `.env.sample`
6. Create test stubs in `tests/conn/$ARGUMENTS/`

All network calls must be async. Use `httpx` or the service's async SDK.
