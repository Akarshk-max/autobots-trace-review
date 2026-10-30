---
name: new-action
description: Scaffold a new action type following the Autobots service pattern. Usage /new-action action_name
user-invocable: true
argument-hint: "[action_name]"
allowed-tools: "Read Glob Grep Edit Write"
---

# Create New Action Type

Scaffold a new action type named `$ARGUMENTS` following the Autobots service folder pattern.

## Steps

1. Look at an existing action type in `src/autobots/action/action_type/` for reference (e.g., `action_text2text/`)
2. Create the new action directory: `src/autobots/action/action_type/action_$ARGUMENTS/`
3. Create files following the service pattern:
   - `action_{name}_doc_model.py` - Pydantic models for input/output
   - `action_{name}.py` - Core implementation with async execution
   - Register in `action_factory.py` and `tool_routes.py` if needed
4. Create a corresponding API endpoint in `src/autobots/api/v1/` if it needs REST access
5. Create test stubs in `tests/action_type/`

Follow the abstract base class pattern in `src/autobots/action/action_type/abc/`.
