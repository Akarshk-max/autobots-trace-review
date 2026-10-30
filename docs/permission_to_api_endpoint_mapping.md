# Connecting Permissions to API Endpoints

**Document type:** Technical Design Reference  
**Audience:** Backend engineers, platform architects, API designers  
**Status:** Canonical  
**Related documents:**
- `authorization_permission_set_design.md` — Permission set decomposition

---

## Table of Contents

1. [Why API Path Is Not the Permission](#1-why-api-path-is-not-the-permission)
2. [The Correct Mental Model](#2-the-correct-mental-model)
3. [Three Layers of the Mapping](#3-three-layers-of-the-mapping)
4. [Permission-to-Endpoint Binding Strategies](#4-permission-to-endpoint-binding-strategies)
   - 4.1 [Decorator-Based Binding (Recommended)](#41-decorator-based-binding-recommended)
   - 4.2 [Middleware-Based Binding](#42-middleware-based-binding)
   - 4.3 [Registry-Based Binding](#43-registry-based-binding)
5. [Mapping Rules](#5-mapping-rules)
   - 5.1 [HTTP Method to Action Mapping](#51-http-method-to-action-mapping)
   - 5.2 [Path Parameter to Resource ID Mapping](#52-path-parameter-to-resource-id-mapping)
   - 5.3 [Query Parameter Considerations](#53-query-parameter-considerations)
6. [Instance-Scoped Endpoints](#6-instance-scoped-endpoints)
7. [Multi-Permission Endpoints](#7-multi-permission-endpoints)
8. [Complete Endpoint-to-Permission Reference Table](#8-complete-endpoint-to-permission-reference-table)
9. [FastAPI Implementation](#9-fastapi-implementation)
   - 9.1 [Core Permission Resolver](#91-core-permission-resolver)
   - 9.2 [Type-Scoped Endpoint Example](#92-type-scoped-endpoint-example)
   - 9.3 [Instance-Scoped Endpoint Example](#93-instance-scoped-endpoint-example)
   - 9.4 [Multi-Permission Endpoint Example](#94-multi-permission-endpoint-example)
   - 9.5 [Permission Registry with Route Introspection](#95-permission-registry-with-route-introspection)
10. [Automatic Permission Discovery from Routes](#10-automatic-permission-discovery-from-routes)
11. [Handling Ownership vs Permission](#11-handling-ownership-vs-permission)
12. [Audit Log Integration](#12-audit-log-integration)
13. [OpenAPI / Swagger Documentation](#13-openapi--swagger-documentation)
14. [Decision Guide](#14-decision-guide)
15. [Common Mistakes](#15-common-mistakes)

---

## 1. Why API Path Is Not the Permission

A common early instinct when designing authorization is to treat the API path itself as the permission key:

```
# Naive approach — DO NOT DO THIS
POST /actions          →  permission: "POST:/actions"
GET  /actions/{id}     →  permission: "GET:/actions/{id}"
DEL  /actions/{id}     →  permission: "DELETE:/actions/{id}"
```

This approach fails for the following reasons:

### 1.1 API Paths Are Unstable

API paths change. A route may be versioned (`/v1/actions` → `/v2/actions`), reorganized, or renamed. If paths are permissions, every rename requires a permission migration and a role policy update. Permissions should be **semantically stable** even when the API surface changes.

```
# v1 route renamed to v2 — permission stays the same
POST /v1/actions    ──┐
POST /v2/actions    ──┴──► action:create   (unchanged)
```

### 1.2 One Route Can Require Multiple Permissions

A single endpoint often requires checking more than one permission. A route that creates an action inside a space requires both `action:create` and `space:read`. The path alone cannot encode that logic.

### 1.3 Path Cannot Encode Instance Context

The path `/actions/abc123/execute` contains a resource ID (`abc123`), but the path string is a template — it describes a shape, not a resolved authorization context. Instance-scoped permissions (`action:execute:abc123`) require resolving the actual resource ID from the path at request time.

### 1.4 Path-Based ACLs Cannot Compose Roles

Roles are a composition of permissions. If permissions are path strings, role definitions must enumerate every path — including future paths that do not exist yet. Semantic permission names allow roles to be defined around capability, not surface area.

### 1.5 Summary

| Dimension | Path-as-permission | Semantic permission |
|-----------|-------------------|--------------------|
| Stability | Breaks on rename | Stable across versions |
| Composability | Cannot stack logically | Roles compose cleanly |
| Instance scope | Cannot express `abc123` | `action:execute:abc123` |
| Multi-check | Cannot express AND/OR | `require_all([p1, p2])` |
| Readability | `DELETE:/v2/actions/{id}` | `action:delete` |
| Audit log | Noisy, path-coupled | Clean, domain-semantics |

---

## 2. The Correct Mental Model

The correct relationship between permissions and API endpoints is:

```
┌─────────────────────────────────────────────────────────────┐
│                    INCOMING REQUEST                          │
│   Method: POST                                               │
│   Path:   /actions/abc123/execute                           │
│   User:   principal_id = user_xyz                           │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│               ENDPOINT METADATA (compile-time)               │
│                                                              │
│   Required permission:  action:execute                       │
│   Resource parameter:   action_id  (from path)               │
│   Scope:                instance                             │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│             PERMISSION RESOLVER  (request-time)              │
│                                                              │
│   Resolved permission:  action:execute:abc123                │
│   Resolution path:                                           │
│     1. Instance grant?  action:execute:abc123  → check DB   │
│     2. Ownership?       owner of abc123        → check DB   │
│     3. Space role?      space includes action:execute        │
│     4. Global role?     platform_admin / superuser           │
└──────────────────────┬──────────────────────────────────────┘
                       │
                 ALLOW or DENY
```

**Key insight:** The API path and HTTP method tell you *what action is being requested*. The permission resolver determines *whether this principal may perform it*. These are two separate concerns with a clean handoff point.

---

## 3. Three Layers of the Mapping

Connecting permissions to API endpoints involves three distinct layers:

```
Layer 1 — Declaration     (what permission does this route require?)
Layer 2 — Extraction      (what resource ID does this request target?)
Layer 3 — Resolution      (does this principal hold that permission for that resource?)
```

### Layer 1: Declaration

Defined at **code/compile time** using decorators, dependency injection, or a route registry. It answers: *what permission is required to call this route at all?*

```python
@app.post("/actions/{action_id}/execute")
async def execute_action(
    action_id: str,
    _: None = Depends(require_permission("action", "execute"))
):
    ...
```

### Layer 2: Extraction

Happens at **request time** inside the permission resolver. It pulls the resource ID from the path, query params, or request body to form the full instance-scoped permission string.

```python
action_id = request.path_params.get("action_id")  # → "abc123"
resolved = f"action:execute:{action_id}"           # → "action:execute:abc123"
```

### Layer 3: Resolution

The resolver walks the grant hierarchy and returns `ALLOW` or `DENY`. It does not know or care about the HTTP method or path — it only works with the resolved permission string and the principal.

---

## 4. Permission-to-Endpoint Binding Strategies

### 4.1 Decorator-Based Binding (Recommended)

The permission requirement is declared directly on the route using a `Depends` factory. This is the cleanest, most explicit, and most reviewable pattern.

**Advantages:**
- Permission is co-located with the route definition.
- Immediately visible to code reviewers.
- `Depends` composition allows stacking.
- FastAPI includes it in the OpenAPI schema if annotated correctly.

**Disadvantages:**
- Permission policy is spread across the codebase (one route at a time).
- Cannot centrally audit all permissions without tooling.

```python
from fastapi import APIRouter, Depends
from app.auth.deps import require_permission, require_instance_permission

router = APIRouter(prefix="/actions", tags=["actions"])

# Type-scoped: can create any action
@router.post("/")
async def create_action(
    body: ActionCreateRequest,
    principal=Depends(require_permission("action", "create"))
):
    ...

# Instance-scoped: can only execute this specific action
@router.post("/{action_id}/execute")
async def execute_action(
    action_id: str,
    body: ActionRunRequest,
    principal=Depends(require_instance_permission("action", "execute", path_param="action_id"))
):
    ...
```

---

### 4.2 Middleware-Based Binding

A middleware intercepts every request and consults a route-to-permission mapping table stored in memory or a config file.

**Advantages:**
- Centralized — all permissions defined in one place.
- Can enforce authorization without touching route handlers.
- Easier to audit and diff.

**Disadvantages:**
- Route matching must replicate FastAPI's own routing logic (fragile).
- Dynamic path params (`{action_id}`) must be extracted manually.
- Less explicit at the route level — a reviewer must look elsewhere.
- Complex routes with conditional permissions are harder to express.

```python
# config/route_permissions.py
ROUTE_PERMISSIONS: list[dict] = [
    {"method": "POST",   "pattern": "/actions",              "permission": "action:create",  "scope": "type"},
    {"method": "GET",    "pattern": "/actions",              "permission": "action:read",    "scope": "type"},
    {"method": "GET",    "pattern": "/actions/{id}",         "permission": "action:read",    "scope": "instance", "param": "id"},
    {"method": "POST",   "pattern": "/actions/{id}/execute", "permission": "action:execute", "scope": "instance", "param": "id"},
    {"method": "DELETE", "pattern": "/actions/{id}",         "permission": "action:delete",  "scope": "instance", "param": "id"},
]
```

```python
# middleware/authorization.py
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from app.auth.resolver import resolve_permission
from config.route_permissions import ROUTE_PERMISSIONS
import re

class PermissionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        for rule in ROUTE_PERMISSIONS:
            # Convert FastAPI-style pattern to regex
            pattern = re.sub(r"\{[^}]+\}", r"([^/]+)", rule["pattern"])
            match = re.fullmatch(pattern, request.url.path)

            if match and request.method == rule["method"]:
                principal = await get_principal(request)
                resource_id = None

                if rule["scope"] == "instance" and rule.get("param"):
                    # Extract positional group from regex match
                    param_names = re.findall(r"\{([^}]+)\}", rule["pattern"])
                    param_index = param_names.index(rule["param"])
                    resource_id = match.group(param_index + 1)

                allowed = await resolve_permission(
                    principal=principal,
                    permission=rule["permission"],
                    resource_id=resource_id
                )

                if not allowed:
                    return JSONResponse({"detail": "Forbidden"}, status_code=403)
                break

        return await call_next(request)
```

---

### 4.3 Registry-Based Binding

A hybrid approach. Routes declare their permission requirement using a lightweight decorator that registers the binding into a central in-memory registry. The registry can be inspected, exported, and used for audit.

```python
# app/auth/registry.py
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class RoutePermission:
    method: str
    path: str
    resource: str
    action: str
    scope: str                      # "type" | "instance"
    path_param: Optional[str] = None
    description: str = ""

PERMISSION_REGISTRY: list[RoutePermission] = []

def register_permission(
    method: str,
    path: str,
    resource: str,
    action: str,
    scope: str = "type",
    path_param: Optional[str] = None,
    description: str = ""
):
    """Register a route-to-permission binding at import time."""
    PERMISSION_REGISTRY.append(RoutePermission(
        method=method,
        path=path,
        resource=resource,
        action=action,
        scope=scope,
        path_param=path_param,
        description=description
    ))

def require_registered_permission(
    method: str,
    path: str,
    resource: str,
    action: str,
    scope: str = "type",
    path_param: Optional[str] = None,
    description: str = ""
):
    """Register + return a Depends factory in one call."""
    register_permission(method, path, resource, action, scope, path_param, description)
    return require_instance_permission(resource, action, path_param=path_param) \
        if scope == "instance" \
        else require_permission(resource, action)
```

```python
# Usage on route
@router.post("/{action_id}/execute")
async def execute_action(
    action_id: str,
    principal=Depends(require_registered_permission(
        method="POST",
        path="/actions/{action_id}/execute",
        resource="action",
        action="execute",
        scope="instance",
        path_param="action_id",
        description="Execute a specific action by ID"
    ))
):
    ...
```

Now you can export the full registry:

```python
# GET /internal/permission-registry
@admin_router.get("/permission-registry")
async def list_permission_registry():
    return [{"method": r.method, "path": r.path,
             "permission": f"{r.resource}:{r.action}",
             "scope": r.scope} for r in PERMISSION_REGISTRY]
```

---

## 5. Mapping Rules

### 5.1 HTTP Method to Action Mapping

The HTTP method provides a strong signal for which action verb to use in the permission string. Establish a platform-wide convention and never deviate from it.

| HTTP Method | Permission Action | Notes |
|-------------|------------------|-------|
| `GET` (collection) | `read` | List all resources of a type |
| `GET` (single) | `read` | Read one resource by ID |
| `POST` (create) | `create` | Standard resource creation |
| `POST` (action) | `execute` | Non-CRUD action on a resource — `execute`, `publish`, `share` |
| `PUT` / `PATCH` | `update` | Full or partial update |
| `DELETE` | `delete` | Hard or soft delete |
| `POST` (share) | `share` | Special — grant access to another principal |
| `POST` (publish) | `publish` | Special — promote to published / deployed state |

**Rule:** If a `POST` route is not a creation route — it performs an action, mutation, or trigger — name the permission verb explicitly. Do not reuse `create` for non-creation routes.

```
POST /actions           →  action:create
POST /actions/{id}/run  →  action:execute    ← NOT action:create
POST /actions/{id}/share →  action:share     ← NOT action:create
```

---

### 5.2 Path Parameter to Resource ID Mapping

Path parameters identify the specific resource instance being acted upon. They are extracted at request time and used to form the instance-scoped permission string.

**Mapping convention:**

```
/actions/{action_id}              →  resource=action,  resource_id=action_id  (from path)
/spaces/{space_id}/actions/{id}   →  resource=action,  resource_id=id         (from path)
                                     scope_resource=space, scope_id=space_id  (context)
```

**Nested resource paths:**

For nested paths (`/spaces/{space_id}/actions/{action_id}`), the **leaf resource** determines the permission. The parent resource (`space_id`) becomes **scope context** that is passed to the resolver.

```python
# /spaces/{space_id}/actions/{action_id} — DELETE
# Permission: action:delete (not space:delete)
# Scope context: space_id = from path

async def require_action_in_space(
    space_id: str,
    action_id: str,
    request: Request,
    principal=Depends(get_principal)
):
    # First verify principal has access to the space
    space_allowed = await resolve_permission(
        principal=principal,
        permission="space:read",
        resource_id=space_id
    )
    if not space_allowed:
        raise HTTPException(status_code=404)  # Do not reveal space existence

    # Then verify action-level permission
    action_allowed = await resolve_permission(
        principal=principal,
        permission="action:delete",
        resource_id=action_id,
        scope_context={"space_id": space_id}  # resolver uses this for space-role check
    )
    if not action_allowed:
        raise HTTPException(status_code=403)

    return principal
```

---

### 5.3 Query Parameter Considerations

Query parameters **never form part of the permission string**. They are filters applied after authorization has passed.

```
GET /actions?space_id=xyz&status=published
         ↑
   This does NOT change the required permission
   Required permission is still: action:read
   space_id is a post-authorization filter on the query
```

**Exception — scope hint parameters:**

Some platforms use query parameters as scope hints, e.g. `?space_id=xyz` to tell the resolver which space to evaluate roles against. This is acceptable as long as:

1. The query param is used as a **resolver input**, not as a permission identifier.
2. The resolver validates that the resource actually belongs to that space.
3. The parameter is never trusted blindly — it must be corroborated against stored data.

```python
async def require_action_read_with_space_hint(
    request: Request,
    action_id: str,
    space_id: Optional[str] = Query(default=None),
    principal=Depends(get_principal)
):
    # Use space_id as resolver hint only — validate it internally
    resolved = await resolve_permission(
        principal=principal,
        permission="action:read",
        resource_id=action_id,
        scope_hint=space_id      # resolver confirms action belongs to this space
    )
    if not resolved:
        raise HTTPException(status_code=403)
```

---

## 6. Instance-Scoped Endpoints

Any endpoint that targets a **specific resource by ID** must use instance-scoped permission resolution. The path parameter is the source of truth for the instance ID.

### 6.1 Identifying Instance-Scoped Routes

A route is instance-scoped if:
- Its path contains a resource ID parameter (`{action_id}`, `{graph_id}`, `{user_id}`).
- The operation is being performed **on** that specific resource.

```
Instance-scoped routes (contain a resource ID):
  GET    /actions/{action_id}            →  action:read:   {action_id}
  PATCH  /actions/{action_id}            →  action:update: {action_id}
  DELETE /actions/{action_id}            →  action:delete: {action_id}
  POST   /actions/{action_id}/execute    →  action:execute:{action_id}
  POST   /actions/{action_id}/publish    →  action:publish:{action_id}
  POST   /actions/{action_id}/share      →  action:share:  {action_id}

Type-scoped routes (no specific resource ID):
  GET    /actions                        →  action:read
  POST   /actions                        →  action:create
```

### 6.2 Resolution for Instance-Scoped Routes

The resolver receives both the permission type and the resource ID. It checks all layers:

```
Permission: action:execute:{action_id}

Step 1 — Direct instance grant
          Query: InstancePermission where principal=user AND permission="action:execute" AND resource_id={action_id}
          → ALLOW if found and not expired

Step 2 — Ownership
          Query: Action where id={action_id} AND owner=user
          → ALLOW if owned AND principal holds action:execute (type-level)

Step 3 — Space role grant
          Query: Space where action {action_id} belongs to space S AND principal has role in S
                 AND role includes action:execute
          → ALLOW if match found

Step 4 — Global role
          → ALLOW if principal has platform_admin or superuser role

Step 5 — Default
          → DENY
```

---

## 7. Multi-Permission Endpoints

Some endpoints require more than one permission simultaneously. There are two patterns:

### 7.1 AND Logic — All Permissions Required

The principal must hold every listed permission. Use for actions that operate across resources.

```python
# Publishing an action to a space requires:
#   - action:publish  (on the specific action)
#   - space:write     (on the destination space)

@router.post("/actions/{action_id}/publish-to-space")
async def publish_action_to_space(
    action_id: str,
    body: PublishRequest,
    principal=Depends(require_all_permissions([
        InstancePermissionRequirement("action", "publish", path_param="action_id"),
        InstancePermissionRequirement("space",  "write",   body_field="space_id"),
    ]))
):
    ...
```

### 7.2 OR Logic — Any Permission Sufficient

The principal must hold at least one of the listed permissions. Use for routes accessible by multiple roles.

```python
# Reading an action can be done by:
#   - The owner (ownership check handles this)
#   - A platform_admin
#   - Holder of action:read:* (wildcard grant)

@router.get("/actions/{action_id}")
async def get_action(
    action_id: str,
    principal=Depends(require_any_permission([
        InstancePermissionRequirement("action", "read", path_param="action_id"),
        TypePermissionRequirement("audit_log", "read"),  # platform_admin can also read
    ]))
):
    ...
```

### 7.3 Mixed Logic — Conditional Permission Based on Request State

Some routes require different permissions depending on the content of the request.

```python
# PATCH /actions/{action_id}
# - Updating name/description → action:update
# - Updating published state  → action:publish  (stronger permission)

@router.patch("/actions/{action_id}")
async def update_action(
    action_id: str,
    body: ActionUpdateRequest,
    request: Request,
    principal=Depends(get_principal)      # just authenticate, check permission below
):
    base_allowed = await resolve_permission(principal, "action:update", resource_id=action_id)
    if not base_allowed:
        raise HTTPException(status_code=403)

    # Escalation check: publishing requires additional permission
    if body.status == "published":
        publish_allowed = await resolve_permission(principal, "action:publish", resource_id=action_id)
        if not publish_allowed:
            raise HTTPException(
                status_code=403,
                detail="Publishing an action requires action:publish permission"
            )
```

---

## 8. Complete Endpoint-to-Permission Reference Table

This table documents the canonical mapping for a platform that manages actions, spaces, graphs, users, and billing. Use it as a template and extend it for your domain.

### Actions

| Method | Path | Permission | Scope | Path Param | Notes |
|--------|------|-----------|-------|-----------|-------|
| `GET` | `/actions` | `action:read` | type | — | Returns only owned/accessible actions |
| `POST` | `/actions` | `action:create` | type | — | |
| `GET` | `/actions/{action_id}` | `action:read` | instance | `action_id` | 404 if no access |
| `PATCH` | `/actions/{action_id}` | `action:update` | instance | `action_id` | |
| `DELETE` | `/actions/{action_id}` | `action:delete` | instance | `action_id` | |
| `POST` | `/actions/{action_id}/execute` | `action:execute` | instance | `action_id` | |
| `POST` | `/actions/{action_id}/publish` | `action:publish` | instance | `action_id` | |
| `POST` | `/actions/{action_id}/share` | `action:share` | instance | `action_id` | |
| `GET` | `/actions/{action_id}/runs` | `action:read` | instance | `action_id` | |
| `GET` | `/actions/{action_id}/runs/{run_id}` | `action:read` | instance | `action_id` | run_id validated after |
| `DELETE` | `/actions/{action_id}/runs/{run_id}` | `action:delete` | instance | `action_id` | |

### Spaces

| Method | Path | Permission | Scope | Path Param | Notes |
|--------|------|-----------|-------|-----------|-------|
| `GET` | `/spaces` | `space:read` | type | — | |
| `POST` | `/spaces` | `space:create` | type | — | |
| `GET` | `/spaces/{space_id}` | `space:read` | instance | `space_id` | |
| `PATCH` | `/spaces/{space_id}` | `space:update` | instance | `space_id` | |
| `DELETE` | `/spaces/{space_id}` | `space:delete` | instance | `space_id` | Owner only |
| `POST` | `/spaces/{space_id}/invite` | `space:manage` | instance | `space_id` | |
| `DELETE` | `/spaces/{space_id}/members/{user_id}` | `space:manage` | instance | `space_id` | |
| `GET` | `/spaces/{space_id}/actions` | `action:read` | type | — | Filtered by space scope |

### Users & Identity

| Method | Path | Permission | Scope | Path Param | Notes |
|--------|------|-----------|-------|-----------|-------|
| `GET` | `/users` | `user:read` | type | — | platform_admin only |
| `GET` | `/users/{user_id}` | `user:read` | instance | `user_id` | Or self |
| `PATCH` | `/users/{user_id}` | `user:update` | instance | `user_id` | Or self |
| `DELETE` | `/users/{user_id}` | `user:delete` | instance | `user_id` | platform_admin only |
| `GET` | `/users/{user_id}/permissions` | `user:read` | instance | `user_id` | Or self |
| `POST` | `/users/{user_id}/roles` | `user:manage` | instance | `user_id` | platform_admin only |

### Billing

| Method | Path | Permission | Scope | Path Param | Notes |
|--------|------|-----------|-------|-----------|-------|
| `GET` | `/billing/subscription` | `billing:read` | type | — | |
| `POST` | `/billing/subscription` | `billing:manage` | type | — | |
| `GET` | `/billing/invoices` | `billing:read` | type | — | |
| `GET` | `/billing/usage` | `billing:read` | type | — | |

### Admin

| Method | Path | Permission | Scope | Path Param | Notes |
|--------|------|-----------|-------|-----------|-------|
| `GET` | `/admin/audit-logs` | `audit_log:read` | type | — | platform_admin+ |
| `GET` | `/admin/users` | `user:read` | type | — | platform_admin+ |
| `POST` | `/admin/users/{user_id}/ban` | `user:manage` | instance | `user_id` | superuser only |
| `GET` | `/internal/permission-registry` | `admin:manage` | type | — | superuser only |

---

## 9. FastAPI Implementation

### 9.1 Core Permission Resolver

```python
# app/auth/resolver.py
from typing import Optional
from app.models import Principal, InstancePermission, Role
from app.db import db

async def resolve_permission(
    principal: Principal,
    permission: str,           # e.g. "action:execute"
    resource_id: Optional[str] = None,   # e.g. "abc123"
    scope_context: Optional[dict] = None  # e.g. {"space_id": "xyz"}
) -> bool:
    """
    Resolve whether a principal holds a given permission.

    Resolution order:
      1. Instance grant (if resource_id provided)
      2. Ownership
      3. Space role (if scope_context or resource has a space)
      4. Global role
    """
    resource, action = permission.split(":", 1)

    # Step 1 — Instance grant
    if resource_id:
        grant = await db.instance_permissions.find_one({
            "principal_id": principal.id,
            "resource": resource,
            "action": action,
            "resource_id": resource_id,
            "$or": [
                {"expires_at": None},
                {"expires_at": {"$gt": datetime.utcnow()}}
            ]
        })
        if grant:
            return True

        # Step 2 — Ownership
        resource_doc = await db[f"{resource}s"].find_one({"_id": resource_id})
        if resource_doc and resource_doc.get("owner_id") == principal.id:
            # Owners always have full access to their own resources
            return True

    # Step 3 — Space role
    space_id = (scope_context or {}).get("space_id")
    if not space_id and resource_id:
        # Try to find which space this resource belongs to
        resource_doc = resource_doc or await db[f"{resource}s"].find_one({"_id": resource_id})
        space_id = (resource_doc or {}).get("space_id")

    if space_id:
        space_member = await db.space_members.find_one({
            "space_id": space_id,
            "principal_id": principal.id
        })
        if space_member:
            role = await db.roles.find_one({"name": space_member["role"]})
            if role and permission in role.get("permissions", []):
                return True

    # Step 4 — Global role
    for role_name in principal.roles:
        role = await db.roles.find_one({"name": role_name})
        if role and (
            permission in role.get("permissions", []) or
            f"{resource}:*" in role.get("permissions", []) or
            "*" in role.get("permissions", [])
        ):
            return True

    return False
```

---

### 9.2 Type-Scoped Endpoint Example

```python
# app/auth/deps.py
from fastapi import Depends, HTTPException, Request
from app.auth.resolver import resolve_permission
from app.auth.principal import get_principal

def require_permission(resource: str, action: str):
    """
    Dependency factory for type-scoped permissions.
    Does not resolve against a specific resource ID.
    """
    async def dependency(
        request: Request,
        principal=Depends(get_principal)
    ):
        allowed = await resolve_permission(
            principal=principal,
            permission=f"{resource}:{action}"
        )
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail=f"Missing permission: {resource}:{action}"
            )
        return principal

    return dependency


# Route usage
@router.post("/actions")
async def create_action(
    body: ActionCreateRequest,
    principal=Depends(require_permission("action", "create"))
):
    ...


@router.get("/actions")
async def list_actions(
    principal=Depends(require_permission("action", "read"))
):
    # Note: list is additionally filtered by ownership/space in the query
    ...
```

---

### 9.3 Instance-Scoped Endpoint Example

```python
# app/auth/deps.py (continued)

def require_instance_permission(
    resource: str,
    action: str,
    path_param: str,          # Name of the FastAPI path parameter holding the resource ID
    return_resource: bool = False  # Optionally return the resource doc to avoid double fetch
):
    """
    Dependency factory for instance-scoped permissions.
    Extracts the resource ID from the named path parameter.
    """
    async def dependency(
        request: Request,
        principal=Depends(get_principal)
    ):
        resource_id = request.path_params.get(path_param)
        if not resource_id:
            raise HTTPException(
                status_code=400,
                detail=f"Missing path parameter: {path_param}"
            )

        allowed = await resolve_permission(
            principal=principal,
            permission=f"{resource}:{action}",
            resource_id=resource_id
        )

        if not allowed:
            # Return 404 for read/delete to avoid confirming resource existence
            status = 404 if action in ("read", "delete") else 403
            raise HTTPException(
                status_code=status,
                detail="Not found" if status == 404 else f"Missing permission: {resource}:{action}:{resource_id}"
            )

        return principal

    return dependency


# Route usage
@router.post("/{action_id}/execute")
async def execute_action(
    action_id: str,
    body: ActionRunRequest,
    principal=Depends(
        require_instance_permission("action", "execute", path_param="action_id")
    )
):
    ...


@router.delete("/{action_id}")
async def delete_action(
    action_id: str,
    principal=Depends(
        require_instance_permission("action", "delete", path_param="action_id")
    )
):
    ...


@router.get("/{action_id}")
async def get_action(
    action_id: str,
    principal=Depends(
        require_instance_permission("action", "read", path_param="action_id")
    )
):
    ...
```

---

### 9.4 Multi-Permission Endpoint Example

```python
# app/auth/deps.py (continued)
from dataclasses import dataclass
from typing import Union

@dataclass
class TypePermReq:
    resource: str
    action: str

@dataclass
class InstancePermReq:
    resource: str
    action: str
    path_param: str

PermReq = Union[TypePermReq, InstancePermReq]


def require_all_permissions(requirements: list[PermReq]):
    """All listed permissions must be satisfied (AND logic)."""
    async def dependency(
        request: Request,
        principal=Depends(get_principal)
    ):
        for req in requirements:
            resource_id = None
            if isinstance(req, InstancePermReq):
                resource_id = request.path_params.get(req.path_param) \
                              or request.query_params.get(req.path_param)

            allowed = await resolve_permission(
                principal=principal,
                permission=f"{req.resource}:{req.action}",
                resource_id=resource_id
            )
            if not allowed:
                raise HTTPException(
                    status_code=403,
                    detail=f"Missing permission: {req.resource}:{req.action}"
                )
        return principal
    return dependency


def require_any_permission(requirements: list[PermReq]):
    """At least one listed permission must be satisfied (OR logic)."""
    async def dependency(
        request: Request,
        principal=Depends(get_principal)
    ):
        for req in requirements:
            resource_id = None
            if isinstance(req, InstancePermReq):
                resource_id = request.path_params.get(req.path_param)

            allowed = await resolve_permission(
                principal=principal,
                permission=f"{req.resource}:{req.action}",
                resource_id=resource_id
            )
            if allowed:
                return principal

        raise HTTPException(
            status_code=403,
            detail="Insufficient permissions"
        )
    return dependency


# Route usage: must have BOTH action:publish on the action AND space:write on the target space
@router.post("/{action_id}/publish-to-space")
async def publish_to_space(
    action_id: str,
    body: PublishToSpaceRequest,
    request: Request,
    principal=Depends(require_all_permissions([
        InstancePermReq("action", "publish", path_param="action_id"),
        InstancePermReq("space",  "write",   path_param="space_id"),   # space_id from query or body
    ]))
):
    ...
```

---

### 9.5 Permission Registry with Route Introspection

```python
# app/auth/registry.py
from fastapi import FastAPI
from app.auth.deps import PERMISSION_REGISTRY

def export_permission_map(app: FastAPI) -> list[dict]:
    """
    Cross-reference FastAPI's route registry with the permission registry.
    Returns a complete permission map for all registered routes.
    """
    route_map = {}
    for route in app.routes:
        if hasattr(route, "methods") and hasattr(route, "path"):
            for method in route.methods:
                key = f"{method}:{route.path}"
                route_map[key] = {
                    "method": method,
                    "path": route.path,
                    "name": route.name,
                    "tags": getattr(route, "tags", [])
                }

    result = []
    for reg in PERMISSION_REGISTRY:
        key = f"{reg.method}:{reg.path}"
        route_info = route_map.get(key, {})
        result.append({
            "method": reg.method,
            "path": reg.path,
            "permission": f"{reg.resource}:{reg.action}",
            "scope": reg.scope,
            "path_param": reg.path_param,
            "description": reg.description,
            "route_name": route_info.get("name"),
            "tags": route_info.get("tags", []),
        })

    # Flag routes that have no registered permission (potential gap)
    registered_keys = {f"{r.method}:{r.path}" for r in PERMISSION_REGISTRY}
    for key, route_info in route_map.items():
        if key not in registered_keys and not route_info["path"].startswith("/internal"):
            result.append({
                "method": route_info["method"],
                "path": route_info["path"],
                "permission": None,
                "scope": None,
                "path_param": None,
                "description": "⚠️  No permission registered for this route",
                "route_name": route_info.get("name"),
                "tags": route_info.get("tags", []),
                "gap": True
            })

    return result
```

---

## 10. Automatic Permission Discovery from Routes

As the API grows, permission coverage gaps become a security risk. Use a startup check that validates every non-internal route has a registered permission.

```python
# app/startup.py
from fastapi import FastAPI
from app.auth.registry import export_permission_map
import logging

logger = logging.getLogger(__name__)

def validate_permission_coverage(app: FastAPI):
    """
    Runs at startup. Logs a warning for any route missing a permission declaration.
    In production, set STRICT_PERMISSION_COVERAGE=true to raise on gaps.
    """
    import os
    strict = os.getenv("STRICT_PERMISSION_COVERAGE", "false").lower() == "true"

    permission_map = export_permission_map(app)
    gaps = [r for r in permission_map if r.get("gap")]

    if gaps:
        for gap in gaps:
            logger.warning(
                f"⚠️  Route has no permission: {gap['method']} {gap['path']}"
            )
        if strict:
            raise RuntimeError(
                f"{len(gaps)} route(s) have no permission registration. "
                f"Fix them or set STRICT_PERMISSION_COVERAGE=false."
            )


# main.py
app = FastAPI()

@app.on_event("startup")
async def startup():
    validate_permission_coverage(app)
```

---

## 11. Handling Ownership vs Permission

Ownership is a special implicit permission that does not need to be explicitly granted. The resolver handles it in Step 2. However, ownership alone is not always sufficient — some actions require explicit grants even for owners.

### Ownership Implies

| Permission | Owner has it? |
|------------|---------------|
| `action:read` | ✅ Always |
| `action:update` | ✅ Always |
| `action:delete` | ✅ Always |
| `action:execute` | ✅ Always |
| `action:publish` | ✅ Always |
| `action:share` | ✅ Always |

### Ownership Does Not Imply

| Permission | Why not? |
|-----------|----------|
| `space:manage` | The space may be owned by someone else; resource just lives there |
| `user:manage` | Never self-grantable |
| `billing:manage` | Requires explicit billing role |
| `audit_log:read` | Requires platform_admin or explicit grant |
| `admin:manage` | Superuser only |

### Transferring Ownership

```python
# PATCH /actions/{action_id}/owner
# Requires: action:share + target user must exist
# Effect: changes owner_id in the resource document

@router.patch("/{action_id}/owner")
async def transfer_ownership(
    action_id: str,
    body: TransferOwnershipRequest,
    principal=Depends(
        require_instance_permission("action", "share", path_param="action_id")
    )
):
    # Only current owner can transfer
    action = await db.actions.find_one({"_id": action_id})
    if action["owner_id"] != principal.id:
        raise HTTPException(status_code=403, detail="Only the current owner can transfer ownership")
    ...
```

---

## 12. Audit Log Integration

Every permission check that results in an access decision should emit an audit event. The event records who tried to do what, on which resource, and whether it was allowed or denied.

### Audit Event Schema

```python
@dataclass
class PermissionAuditEvent:
    event_type: str          # "permission.allow" | "permission.deny"
    principal_id: str
    permission: str          # e.g. "action:execute"
    resource_id: str | None  # e.g. "abc123"
    resolved_via: str        # "instance_grant" | "ownership" | "space_role" | "global_role" | "deny"
    request_method: str      # HTTP method
    request_path: str        # API path
    timestamp: str           # ISO 8601
    request_id: str          # from X-Request-ID header
    client_ip: str
```

### Emitting Audit Events from the Resolver

```python
async def resolve_permission(
    principal: Principal,
    permission: str,
    resource_id: Optional[str] = None,
    request: Optional[Request] = None,
    **kwargs
) -> bool:
    resource, action = permission.split(":", 1)

    # ... resolution logic from section 9.1 ...

    resolved_via = "deny"
    result = False

    # Step 1
    if await check_instance_grant(principal.id, resource, action, resource_id):
        resolved_via, result = "instance_grant", True

    # Step 2
    elif await check_ownership(principal.id, resource, resource_id):
        resolved_via, result = "ownership", True

    # Step 3
    elif await check_space_role(principal.id, permission, resource_id):
        resolved_via, result = "space_role", True

    # Step 4
    elif await check_global_role(principal, permission):
        resolved_via, result = "global_role", True

    # Emit audit event
    await audit_logger.emit(PermissionAuditEvent(
        event_type="permission.allow" if result else "permission.deny",
        principal_id=principal.id,
        permission=permission,
        resource_id=resource_id,
        resolved_via=resolved_via,
        request_method=request.method if request else "unknown",
        request_path=str(request.url.path) if request else "unknown",
        timestamp=datetime.utcnow().isoformat(),
        request_id=request.headers.get("x-request-id", "") if request else "",
        client_ip=request.client.host if request else ""
    ))

    return result
```

---

## 13. OpenAPI / Swagger Documentation

Because permissions are defined in `Depends`, FastAPI does not automatically describe them in the OpenAPI spec. Add a custom extension to expose permission metadata in the generated schema.

### Approach: Custom OpenAPI Extension

```python
# app/auth/openapi.py

def permission_docs(
    permission: str,
    scope: str,
    description: str = ""
) -> dict:
    """
    Returns an openapi_extra dict for use in route decorators.
    Adds x-permission extension to the OpenAPI spec.
    """
    return {
        "x-permission": {
            "required": permission,
            "scope": scope,
            "description": description or f"Requires {permission} permission"
        }
    }
```

```python
# Route with OpenAPI documentation
@router.post(
    "/{action_id}/execute",
    summary="Execute an action",
    description="Run a specific action by its ID.",
    openapi_extra=permission_docs(
        permission="action:execute",
        scope="instance",
        description="Caller must hold action:execute on this specific action ID, or own the action."
    )
)
async def execute_action(
    action_id: str,
    body: ActionRunRequest,
    principal=Depends(
        require_instance_permission("action", "execute", path_param="action_id")
    )
):
    ...
```

The resulting OpenAPI spec will include:

```json
"x-permission": {
    "required": "action:execute",
    "scope": "instance",
    "description": "Caller must hold action:execute on this specific action ID, or own the action."
}
```

You can render this in documentation portals (Swagger UI custom plugins, Redoc extensions, or internal API docs) to show consumers exactly what permission each endpoint requires.

---

## 14. Decision Guide

Use this guide when deciding how to bind a permission to a new endpoint.

```
Step 1: Does the route target a specific resource by ID?
   ├── YES → Does the path have an ID param ({action_id}, {graph_id}, ...)?    
   │          ├── YES → Use require_instance_permission(resource, action, path_param="...")
   │          └── NO  → The ID comes from query/body → extract manually in the Depends
   └── NO  → Use require_permission(resource, action)  [type-scoped]

Step 2: Does the route require permissions on more than one resource?
   ├── YES, all required   → Use require_all_permissions([...])
   ├── YES, any sufficient → Use require_any_permission([...])
   └── NO                  → Use single Depends

Step 3: Does the required permission change depending on request content?
   ├── YES → Use get_principal() + manual resolve_permission() calls inside the handler
   └── NO  → Use a Depends factory

Step 4: What HTTP status to return on denial?
   ├── action in ("read", "delete") → 404  (do not reveal resource existence)
   └── action in ("update", "execute", "publish", "share", "manage") → 403

Step 5: Should the route be in the permission registry?
   ├── YES for all production routes
   └── NO  for internal/debug routes only — mark them explicitly
```

---

## 15. Common Mistakes

### Mistake 1 — Using HTTP method as the permission key

```python
# ❌ Wrong
if request.method == "DELETE":
    check_permission("DELETE")

# ✅ Correct
require_instance_permission("action", "delete", path_param="action_id")
```

---

### Mistake 2 — Checking permission inside the route handler after loading the resource

```python
# ❌ Wrong — loads the resource before checking permission
async def get_action(action_id: str):
    action = await db.actions.find_one({"_id": action_id})  # DB hit before auth check
    if action["owner_id"] != current_user.id:
        raise HTTPException(403)
    return action

# ✅ Correct — permission checked in Depends before handler body runs
async def get_action(
    action_id: str,
    principal=Depends(require_instance_permission("action", "read", path_param="action_id"))
):
    action = await db.actions.find_one({"_id": action_id})
    return action
```

---

### Mistake 3 — Returning 403 for read/delete when resource does not exist or is not visible

```python
# ❌ Wrong — tells the caller the resource exists but they cannot access it
if not allowed:
    raise HTTPException(403)

# ✅ Correct — return 404 for read/delete to avoid leaking resource existence
if action in ("read", "delete") and not allowed:
    raise HTTPException(404, detail="Not found")
```

---

### Mistake 4 — Trusting path params without resolving them against the DB

```python
# ❌ Wrong — assumes the resource actually belongs to the claimed space
# /spaces/{space_id}/actions/{action_id}
if principal_has_space_role(space_id):  # space_id from path, not validated against the action
    return True

# ✅ Correct — validate that the action actually belongs to the space
action = await db.actions.find_one({"_id": action_id})
if action["space_id"] != space_id:
    raise HTTPException(404)  # action exists but not in this space
```

---

### Mistake 5 — Skipping permission registration for "internal" routes that aren't truly internal

If a route is accessible without authentication, mark it explicitly:

```python
@router.get("/health", include_in_schema=False)  # public, no permission needed
async def health_check():
    return {"status": "ok"}

# Mark clearly in registry so the coverage checker ignores it
PUBLIC_ROUTES = [
    "GET:/health",
    "GET:/docs",
    "GET:/openapi.json"
]
```

---

### Mistake 6 — Using query parameters as part of the permission key

```python
# ❌ Wrong
permission = f"action:read?status=published"  # query params are not part of permission

# ✅ Correct
permission = "action:read"  # resolve, then filter results by status in the query
results = await db.actions.find({"status": "published", ...})
```

---

### Mistake 7 — Not emitting audit events for DENY decisions

Denied access events are often more operationally important than allowed ones. Always audit both.

```python
# ❌ Wrong — only logs allows
if result:
    await audit(event_type="permission.allow", ...)

# ✅ Correct — log both
await audit(event_type="permission.allow" if result else "permission.deny", ...)
```

---

*End of document.*
