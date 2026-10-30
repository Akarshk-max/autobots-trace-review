---
paths:
  - "src/autobots/api/**/*.py"
---

# API Endpoint Rules

- All endpoints use FastAPI dependency injection for auth (JWT via Supabase)
- Request/response models must be Pydantic BaseModel subclasses
- Endpoints go in `src/autobots/api/v1/api_{domain}/` directories
- Router is included in `src/autobots/api/v1/v1.py`
- Middleware execution order: context injection → blacklist check → exception handler → usage tracking
- Multi-tenant: always scope queries by user_id from Context
