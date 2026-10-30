---
paths:
  - "src/autobots/conn/**/*.py"
---

# Connector Rules

- All external service calls must be async
- Store credentials in Settings (Pydantic BaseSettings) via environment variables
- Each connector is self-contained in `src/autobots/conn/{service}/`
- Use the service's async SDK when available, otherwise httpx
- Handle rate limits and retries at the connector level
- Never hardcode API keys, URLs, or credentials
