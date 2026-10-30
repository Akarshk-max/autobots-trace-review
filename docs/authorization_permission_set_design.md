# Authorization Layer — Decomposing Platform Capabilities into a Permission Set

> **Document Type:** Technical Design Reference  
> **Audience:** Platform engineers, backend developers, security architects  
> **Status:** Draft  
> **Last Updated:** 2025

---

## Table of Contents

- [1. Purpose and Scope](#1-purpose-and-scope)
- [2. Core Concepts](#2-core-concepts)
- [3. Decomposition Methodology](#3-decomposition-methodology)
- [4. Step 1 — Identify Platform Domains](#4-step-1--identify-platform-domains)
- [5. Step 2 — Enumerate Resources per Domain](#5-step-2--enumerate-resources-per-domain)
- [6. Step 3 — Define Actions per Resource](#6-step-3--define-actions-per-resource)
- [7. Step 4 — Compose Permissions](#7-step-4--compose-permissions)
- [8. Step 5 — Group Permissions into Permission Sets (Roles)](#8-step-5--group-permissions-into-permission-sets-roles)
- [9. Step 6 — Apply Scope Boundaries](#9-step-6--apply-scope-boundaries)
- [10. Permission Naming Convention](#10-permission-naming-convention)
- [11. Permission Set Reference Table](#11-permission-set-reference-table)
- [12. Enforcement Model](#12-enforcement-model)
- [13. FastAPI Integration Pattern](#13-fastapi-integration-pattern)
- [14. Resource-Instance Permissions](#14-resource-instance-permissions)
- [15. Design Principles and Tradeoffs](#15-design-principles-and-tradeoffs)
- [16. Common Pitfalls](#16-common-pitfalls)

---

## 1. Purpose and Scope

This document describes how to systematically decompose platform capabilities into a **permission set** — a structured collection of fine-grained rules that governs what an authenticated principal can do, on which resources, within which context.

A well-designed permission set enables:

- **Least-privilege access** — principals can only do exactly what they need.
- **Auditability** — every action can be traced to an explicit permission.
- **Flexibility** — new roles can be assembled from existing permissions without rewriting logic.
- **Separation of concerns** — authentication (who are you?) is decoupled from authorization (what can you do?).

This guide produces a schema that can be enforced at the API layer, the service layer, or the data layer depending on the sensitivity of the resource.

---

## 2. Core Concepts

Before decomposing capabilities, establish shared vocabulary.

| Term | Definition |
|------|------------|
| **Principal** | The entity making a request — a user, service account, API key, or system agent. |
| **Resource** | A platform entity that can be acted upon — an action, a graph, a file, a space, a user account. |
| **Action** | An operation that can be performed on a resource — `create`, `read`, `update`, `delete`, `execute`, `share`. |
| **Permission** | A single rule binding a principal to an action on a resource type: `action:read`. |
| **Permission Set** | A named, reusable collection of permissions assigned to a role or a principal. |
| **Role** | A semantic identity that maps to a permission set — `owner`, `admin`, `member`, `viewer`. |
| **Scope** | The boundary within which a permission applies — `global`, `space`, `user`, `resource-instance`. |
| **Policy** | The runtime decision: does principal P hold permission X on resource R in scope S? |

---

## 3. Decomposition Methodology

The process moves from the broadest view (platform domains) down to the finest-grained unit (a single permission), then back up to compose permission sets.

```
Platform
  └── Domains
        └── Resources
              └── Actions
                    └── Permissions
                          └── Permission Sets (Roles)
                                └── Scoped Policy Enforcement
```

Each step is described in detail in the sections below.

---

## 4. Step 1 — Identify Platform Domains

A **domain** is a high-level capability area of the platform. Group all platform features into domains before going further. Domains map naturally to services, modules, or top-level route prefixes in your API.

### How to identify domains

1. List every top-level concept users interact with.
2. Group related concepts under a single domain label.
3. Verify each domain has a clear owner (team or module).
4. Avoid domains that are too coarse ("everything") or too fine ("individual endpoint").

### Example Domains for an AI Platform

| Domain | Description |
|--------|-------------|
| `actions` | AI action definitions, configuration, and execution. |
| `action_runs` | Execution history, results, and conversation state. |
| `graphs` | Action graph definitions and run orchestration. |
| `spaces` | Workspace isolation boundaries and collaboration. |
| `users` | User accounts, profiles, and identity management. |
| `files` | Uploaded files, storage, and retrieval. |
| `integrations` | Third-party app connections and credentials. |
| `billing` | Plans, subscriptions, usage quotas, and invoices. |
| `admin` | Platform-level configuration and oversight. |
| `analytics` | Usage metrics, logs, and reporting. |

> **Rule:** A resource should belong to exactly one domain. If you cannot decide, the domain boundary is probably wrong — split or merge accordingly.

---

## 5. Step 2 — Enumerate Resources per Domain

A **resource** is anything in the platform that can be created, owned, shared, or acted upon. For each domain from Step 1, list every concrete resource type.

### How to enumerate resources

1. Walk through every API endpoint and identify the noun it acts on.
2. Walk through the data model and identify every top-level document or entity.
3. Ask: "Can a user own this? Can it be shared? Should access to it be restricted?" — if yes to any, it is a resource.

### Example Resources by Domain

| Domain | Resources |
|--------|-----------|
| `actions` | `action`, `action_config`, `action_version` |
| `action_runs` | `action_run`, `action_run_result`, `action_run_log` |
| `graphs` | `graph`, `graph_run`, `graph_node` |
| `spaces` | `space`, `space_membership` |
| `users` | `user`, `user_profile`, `api_key` |
| `files` | `file`, `file_metadata`, `vector_store` |
| `integrations` | `app_auth`, `credential`, `oauth_token` |
| `billing` | `subscription`, `invoice`, `usage_record` |
| `admin` | `platform_config`, `feature_flag`, `audit_log` |
| `analytics` | `usage_report`, `event_log` |

---

## 6. Step 3 — Define Actions per Resource

An **action** is an operation a principal can perform on a resource. Use a standard action vocabulary across all resources for consistency. Extend it only when truly necessary.

### Standard Action Vocabulary

| Action | Meaning |
|--------|---------|
| `create` | Instantiate a new resource. |
| `read` | Retrieve or view an existing resource. |
| `update` | Modify attributes of an existing resource. |
| `delete` | Permanently remove a resource. |
| `list` | Retrieve a collection of resources. |
| `execute` | Trigger a side-effecting operation (run an action, trigger a graph). |
| `share` | Grant another principal access to a resource. |
| `publish` | Make a resource visible to a broader audience or externally. |
| `manage` | Administrative control — includes invite, promote, configure, override. |
| `export` | Extract data from a resource to an external format. |
| `import` | Ingest external data into a resource. |

### Mapping Actions to Resources (Examples)

| Resource | Applicable Actions |
|----------|-------------------|
| `action` | `create`, `read`, `update`, `delete`, `list`, `execute`, `share`, `publish` |
| `action_run` | `read`, `list`, `delete` |
| `graph` | `create`, `read`, `update`, `delete`, `list`, `execute`, `share` |
| `space` | `create`, `read`, `update`, `delete`, `manage` |
| `space_membership` | `create`, `read`, `delete`, `manage` |
| `file` | `create`, `read`, `delete`, `list`, `share`, `export` |
| `app_auth` | `create`, `read`, `delete`, `list` |
| `api_key` | `create`, `read`, `delete`, `list` |
| `subscription` | `read`, `update` |
| `invoice` | `read`, `list`, `export` |
| `audit_log` | `read`, `list`, `export` |
| `platform_config` | `read`, `update` |
| `feature_flag` | `read`, `update` |

> **Rule:** Do not apply every action to every resource by default. Only map the actions that make business sense. Unused mappings create dead permission entries that confuse auditors.

---

## 7. Step 4 — Compose Permissions

A **permission** is a flat, atomic string formed by combining the resource type and the action. It represents the smallest grantable unit of access.

### Permission String Formats

There are two valid forms of a permission string. Choose the form based on the scope of access being granted.

#### Form 1 — Type-Scoped Permission

Applies to **all resources of a given type** that the principal is otherwise allowed to access (via ownership or space membership).

```
<resource>:<action>
```

Examples:

```
action:execute        # Can execute any action the principal owns or has access to
action:read           # Can read any accessible action
graph:share           # Can share any accessible graph
file:delete           # Can delete any accessible file
```

#### Form 2 — Instance-Scoped Permission

Applies to **one specific resource** identified by its unique ID. This overrides type-scope — a principal may hold `action:execute:<action_id>` without holding `action:execute` globally.

```
<resource>:<action>:<resource_id>
```

Examples:

```
action:execute:abc123         # Can execute only this specific action
action:read:abc123            # Can read only this specific action
action:share:abc123           # Can share only this specific action
graph:execute:graph_xyz       # Can execute only this specific graph
file:read:file_001            # Can read only this specific file
```

> **When to use which form:**
>
> | Situation | Use |
> |-----------|-----|
> | Granting access to all resources a user creates | Type-scoped: `action:execute` |
> | Sharing a specific action with another user | Instance-scoped: `action:execute:<action_id>` |
> | Publishing an action to a space | Type-scoped + space scope boundary |
> | Restricting a service account to one resource | Instance-scoped: `action:execute:<action_id>` |

### Full Permission String Examples

```
# Type-scoped
action:create
action:read
action:update
action:delete
action:list
action:execute
action:share
action:publish

action_run:read
action_run:list
action_run:delete

graph:create
graph:read
graph:execute
graph:share

space:create
space:manage

space_membership:manage

file:create
file:read
file:delete
file:share

app_auth:create
app_auth:read
app_auth:delete

api_key:create
api_key:delete

subscription:read
subscription:update

invoice:read
invoice:export

audit_log:read
audit_log:export

platform_config:update
feature_flag:update

# Instance-scoped
action:execute:abc123
action:read:abc123
action:share:abc123
graph:execute:graph_xyz
file:read:file_001
```

### Wildcard Shorthand (for internal/admin use only)

For convenience in configuration only — never evaluate wildcards at runtime without expanding them first:

```
action:*               # All actions on the action resource type
*:read                 # Read access across all resource types
*:*                    # Superuser — use with extreme caution
action:execute:*       # Execute any action instance — use carefully
```

---

## 8. Step 5 — Group Permissions into Permission Sets (Roles)

A **permission set** (also called a role) is a named, reusable bundle of permissions. Roles should reflect real business identities, not technical implementation details.

### Design Rules for Permission Sets

1. **Name roles by intent**, not by access level. Prefer `content_creator` over `level_2_user`.
2. **Use role inheritance** sparingly — deep hierarchies are hard to reason about.
3. **Start with the minimum** — add permissions only when a real use case demands it.
4. **Document every permission** in a role and why it was included.
5. **Separate platform roles from resource roles** — a user can be a `platform:member` globally and a `space:owner` within a specific space.

### Permission Set Definitions

---

#### `viewer`

Read-only access to shared and public resources. Cannot create, modify, or execute anything.

```
action:read
action:list
action_run:read
action_run:list
graph:read
graph:list
file:read
file:list
```

---

#### `member`

Standard platform user. Can create and manage their own resources. Cannot manage other users or platform configuration.

Inherits: `viewer`

Additional permissions:

```
action:create
action:update
action:delete
action:execute
action_run:delete
graph:create
graph:update
graph:delete
graph:execute
file:create
file:delete
app_auth:create
app_auth:read
app_auth:delete
api_key:create
api_key:delete
subscription:read
invoice:read
```

---

#### `space_admin`

Manages a specific Space. Can manage members within the Space and access all Space-scoped resources. Does not have platform-level admin access.

Inherits: `member`

Additional permissions:

```
space:manage
space_membership:manage
action:share
graph:share
file:share
```

---

#### `space_owner`

Full control over a specific Space including deletion and owner transfer.

Inherits: `space_admin`

Additional permissions:

```
space:delete
action:publish
```

---

#### `platform_admin`

Platform-level operator. Can manage users, spaces, configurations, and billing. Does not have destructive superuser access by default.

Inherits: `member`

Additional permissions:

```
space:create
space:manage
space:delete
space_membership:manage
subscription:update
invoice:export
audit_log:read
audit_log:export
platform_config:read
platform_config:update
feature_flag:read
feature_flag:update
```

---

#### `superuser`

Internal system identity only. Not assignable to human users in production. Used for inter-service calls and migrations.

```
*:*
```

> ⚠️ **Superuser must never be granted via the public API or UI.** It is for internal service accounts only.

---

## 9. Step 6 — Apply Scope Boundaries

A permission alone is not enough to make a decision. The same permission can mean different things depending on **scope** — the boundary within which it is effective.

### Scope Hierarchy

```
global
  └── space
        └── user
              └── resource-instance
```

| Scope | Meaning | Example |
|-------|---------|------|
| `global` | Applies across the entire platform regardless of space or user. | Platform admins, superusers. |
| `space` | Applies within a specific Space only. | A user is `space_admin` in Space A but only `member` in Space B. |
| `user` | Applies only to resources owned by that user. | A member can `action:delete` only their own actions, not others'. |
| `resource-instance` | Applies to a single specific resource identified by ID. | A user has `action:execute` on action ID `abc123` only because it was explicitly shared. |

### Scoped Permission Representation

At evaluation time, a permission check always includes scope:

```python
class PermissionCheck:
    principal_id: str          # Who is asking
    permission: str            # What they want to do: "action:execute"
    resource_id: str | None    # Which specific resource (if applicable)
    scope_type: str            # "global" | "space" | "user" | "instance"
    scope_id: str | None       # Space ID, User ID, or Resource Instance ID
```

### Scope Resolution Order

When evaluating a permission, check from narrowest to broadest:

1. **Resource-instance grant** — does the principal hold an explicit instance-scoped permission such as `action:execute:<action_id>`? This is the most specific check and is evaluated first.
2. **User-scope grant** — does the principal own this resource? Ownership implies type-scoped permissions such as `action:execute`.
3. **Space-scope grant** — does the principal hold a role within the Space that contains this resource, and does that role include the required type-scoped permission?
4. **Global grant** — does the principal hold a platform-level role that covers this type-scoped permission?

If any level resolves to `ALLOW`, the request proceeds.  
If all levels resolve to `DENY` or `NOT_FOUND`, the request is rejected with `403 Forbidden`.

### Resolution Example — `action:execute` on action `abc123`

```
Principal: user_99
Requested: POST /actions/abc123/run
Required permission: action:execute
Resource owner: user_42

Step 1 — Resource-instance check:
  Does user_99 hold "action:execute:abc123"?   → YES (user_42 shared it)
  → ALLOW. Stop here.

---

# If Step 1 had returned NO:

Step 2 — Ownership check:
  Is user_99 the owner of action abc123?        → NO
  → Continue.

Step 3 — Space-scope check:
  Is action abc123 published to a Space?
  Does user_99 have a role in that Space with action:execute?  → NO
  → Continue.

Step 4 — Global role check:
  Does user_99 hold platform_admin or superuser? → NO
  → DENY. Return 403 Forbidden.
```

> **Key point:** Instance-scoped grants (`action:execute:<id>`) allow fine-grained sharing without elevating the principal's global or space-level role. A user can be permitted to execute exactly one action without gaining the ability to execute any other action on the platform.

---

## 10. Permission Naming Convention

Consistent naming is critical for maintainability. Adopt one convention and enforce it across all code, config, and documentation.

### Rules

| Rule | Good | Bad |
|------|------|-----|
| Always lowercase | `action:read` | `Action:Read` |
| Use singular nouns | `action:create` | `actions:create` |
| Use verb for action part | `action:execute` | `action:execution` |
| Use colon as separator | `action:read` | `action.read`, `action/read` |
| Use underscore in multi-word resources | `action_run:list` | `actionRun:list`, `action-run:list` |
| No abbreviations | `subscription:read` | `sub:read` |
| No version in permission string | `action:read` | `action_v2:read` |

### Wildcard and Alias Conventions

| Pattern | Meaning | Use |
|---------|---------|-----|
| `<resource>:*` | All actions on a resource | Admin config only |
| `*:read` | Read all resources | Admin config only |
| `*:*` | Full platform access | Superuser service accounts only |

---

## 11. Permission Set Reference Table

This table summarizes which roles hold which permissions. Use this as the source of truth when implementing the enforcement layer.

| Permission | viewer | member | space_admin | space_owner | platform_admin |
|------------|:------:|:------:|:-----------:|:-----------:|:--------------:|
| `action:create` | | ✓ | ✓ | ✓ | ✓ |
| `action:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `action:update` | | ✓ | ✓ | ✓ | ✓ |
| `action:delete` | | ✓ | ✓ | ✓ | ✓ |
| `action:list` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `action:execute` | | ✓ | ✓ | ✓ | ✓ |
| `action:share` | | | ✓ | ✓ | |
| `action:publish` | | | | ✓ | |
| `action_run:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `action_run:list` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `action_run:delete` | | ✓ | ✓ | ✓ | ✓ |
| `graph:create` | | ✓ | ✓ | ✓ | ✓ |
| `graph:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `graph:update` | | ✓ | ✓ | ✓ | ✓ |
| `graph:delete` | | ✓ | ✓ | ✓ | ✓ |
| `graph:execute` | | ✓ | ✓ | ✓ | ✓ |
| `graph:share` | | | ✓ | ✓ | |
| `space:create` | | | | | ✓ |
| `space:manage` | | | ✓ | ✓ | ✓ |
| `space:delete` | | | | ✓ | ✓ |
| `space_membership:manage` | | | ✓ | ✓ | ✓ |
| `file:create` | | ✓ | ✓ | ✓ | ✓ |
| `file:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `file:delete` | | ✓ | ✓ | ✓ | ✓ |
| `file:share` | | | ✓ | ✓ | |
| `app_auth:create` | | ✓ | ✓ | ✓ | ✓ |
| `app_auth:read` | | ✓ | ✓ | ✓ | ✓ |
| `app_auth:delete` | | ✓ | ✓ | ✓ | ✓ |
| `api_key:create` | | ✓ | ✓ | ✓ | ✓ |
| `api_key:delete` | | ✓ | ✓ | ✓ | ✓ |
| `subscription:read` | | ✓ | ✓ | ✓ | ✓ |
| `subscription:update` | | | | | ✓ |
| `invoice:read` | | ✓ | ✓ | ✓ | ✓ |
| `invoice:export` | | | | | ✓ |
| `audit_log:read` | | | | | ✓ |
| `audit_log:export` | | | | | ✓ |
| `platform_config:read` | | | | | ✓ |
| `platform_config:update` | | | | | ✓ |
| `feature_flag:read` | | | | | ✓ |
| `feature_flag:update` | | | | | ✓ |

---

## 12. Enforcement Model

Permissions must be enforced consistently at every entry point where a principal acts on a resource.

### Enforcement Layers

```
Request
  │
  ▼
┌──────────────────────────┐
│   Authentication Layer   │  ← Who are you? Verify identity token / API key.
└──────────┬───────────────┘
           │
           ▼
┌──────────────────────────┐
│   Authorization Layer    │  ← What can you do? Resolve role → permission set.
│  (Permission Evaluator)  │
└──────────┬───────────────┘
           │
           ▼
┌──────────────────────────┐
│   Scope Filter           │  ← On which resources? Apply ownership / space / instance scope.
└──────────┬───────────────┘
           │
           ▼
┌──────────────────────────┐
│   Handler / Service      │  ← Execute the operation.
└──────────────────────────┘
```

### Enforcement Decision Logic

```python
def is_allowed(principal, permission, resource, scope) -> bool:
    """
    Returns True if the principal holds the given permission
    on the given resource within the given scope.
    """
    # 1. Resolve the principal's permission set(s) for this scope
    permission_sets = resolve_permission_sets(principal, scope)

    # 2. Expand all permission sets into a flat set of permissions
    granted = expand_permissions(permission_sets)

    # 3. Check if the required permission is granted
    if permission not in granted:
        return False

    # 4. Check ownership and scope boundaries
    return scope_filter(principal, resource, scope)
```

### Denial Response Codes

| Condition | HTTP Code | Message |
|-----------|-----------|-----|
| No valid identity token | `401 Unauthorized` | Authentication required. |
| Valid identity but insufficient permission | `403 Forbidden` | You do not have permission to perform this action. |
| Resource exists but is in an inaccessible scope | `404 Not Found` | Prefer `404` over `403` to avoid resource enumeration. |

> **Security Note:** For resources that exist but are outside the caller's scope, returning `404 Not Found` instead of `403 Forbidden` prevents principals from inferring the existence of resources they cannot access.

---

## 13. FastAPI Integration Pattern

This section shows how to integrate the permission set model into a FastAPI application using `Depends`.

### Permission Model

```python
# src/autobots/auth/permissions.py

from enum import Enum

class Permission(str, Enum):
    ACTION_CREATE      = "action:create"
    ACTION_READ        = "action:read"
    ACTION_UPDATE      = "action:update"
    ACTION_DELETE      = "action:delete"
    ACTION_LIST        = "action:list"
    ACTION_EXECUTE     = "action:execute"
    ACTION_SHARE       = "action:share"
    ACTION_PUBLISH     = "action:publish"

    ACTION_RUN_READ    = "action_run:read"
    ACTION_RUN_LIST    = "action_run:list"
    ACTION_RUN_DELETE  = "action_run:delete"

    GRAPH_CREATE       = "graph:create"
    GRAPH_READ         = "graph:read"
    GRAPH_EXECUTE      = "graph:execute"
    GRAPH_SHARE        = "graph:share"

    SPACE_CREATE       = "space:create"
    SPACE_MANAGE       = "space:manage"
    SPACE_DELETE       = "space:delete"
    SPACE_MEMBER_MANAGE = "space_membership:manage"

    FILE_CREATE        = "file:create"
    FILE_READ          = "file:read"
    FILE_DELETE        = "file:delete"
    FILE_SHARE         = "file:share"

    AUDIT_LOG_READ     = "audit_log:read"
    AUDIT_LOG_EXPORT   = "audit_log:export"

    PLATFORM_CONFIG_UPDATE = "platform_config:update"
    FEATURE_FLAG_UPDATE    = "feature_flag:update"
```

### Permission Set Registry

```python
# src/autobots/auth/permission_sets.py

from autobots.auth.permissions import Permission

PERMISSION_SETS: dict[str, set[Permission]] = {
    "viewer": {
        Permission.ACTION_READ,
        Permission.ACTION_LIST,
        Permission.ACTION_RUN_READ,
        Permission.ACTION_RUN_LIST,
        Permission.GRAPH_READ,
        Permission.FILE_READ,
    },
    "member": {
        # Inherits viewer
        *PERMISSION_SETS["viewer"],   # expand inline or handle via resolver
        Permission.ACTION_CREATE,
        Permission.ACTION_UPDATE,
        Permission.ACTION_DELETE,
        Permission.ACTION_EXECUTE,
        Permission.ACTION_RUN_DELETE,
        Permission.GRAPH_CREATE,
        Permission.GRAPH_EXECUTE,
        Permission.FILE_CREATE,
        Permission.FILE_DELETE,
    },
    "space_admin": {
        *PERMISSION_SETS["member"],
        Permission.SPACE_MANAGE,
        Permission.SPACE_MEMBER_MANAGE,
        Permission.ACTION_SHARE,
        Permission.GRAPH_SHARE,
        Permission.FILE_SHARE,
    },
    "space_owner": {
        *PERMISSION_SETS["space_admin"],
        Permission.SPACE_DELETE,
        Permission.ACTION_PUBLISH,
    },
    "platform_admin": {
        *PERMISSION_SETS["member"],
        Permission.SPACE_CREATE,
        Permission.SPACE_MANAGE,
        Permission.SPACE_DELETE,
        Permission.SPACE_MEMBER_MANAGE,
        Permission.AUDIT_LOG_READ,
        Permission.AUDIT_LOG_EXPORT,
        Permission.PLATFORM_CONFIG_UPDATE,
        Permission.FEATURE_FLAG_UPDATE,
    },
}
```

### `Depends`-Based Permission Guard

```python
# src/autobots/auth/depends.py

from fastapi import Depends, HTTPException, Request, status
from autobots.auth.permissions import Permission
from autobots.auth.permission_sets import PERMISSION_SETS
from autobots.auth.context import resolve_principal  # your auth logic


def require_permission(permission: Permission):
    """
    Returns a FastAPI dependency that enforces a single permission.

    Usage:
        @router.post("/actions")
        async def create_action(
            _: None = Depends(require_permission(Permission.ACTION_CREATE))
        ):
            ...
    """
    async def _check(request: Request) -> None:
        principal = await resolve_principal(request)  # user + roles from JWT/session

        # Collect all permissions held by the principal
        granted: set[Permission] = set()
        for role in principal.roles:
            granted.update(PERMISSION_SETS.get(role, set()))

        if permission not in granted:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {permission.value}",
            )

    return Depends(_check)


def require_any_permission(*permissions: Permission):
    """
    Passes if the principal holds at least one of the given permissions.
    """
    async def _check(request: Request) -> None:
        principal = await resolve_principal(request)

        granted: set[Permission] = set()
        for role in principal.roles:
            granted.update(PERMISSION_SETS.get(role, set()))

        if not granted.intersection(permissions):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions.",
            )

    return Depends(_check)


def require_all_permissions(*permissions: Permission):
    """
    Passes only if the principal holds all of the given permissions.
    """
    async def _check(request: Request) -> None:
        principal = await resolve_principal(request)

        granted: set[Permission] = set()
        for role in principal.roles:
            granted.update(PERMISSION_SETS.get(role, set()))

        missing = set(permissions) - granted
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing permissions: {[p.value for p in missing]}",
            )

    return Depends(_check)
```

### Route-Level Usage

```python
# src/autobots/action/action_router.py

from fastapi import APIRouter, Depends
from autobots.auth.depends import require_permission
from autobots.auth.permissions import Permission

router = APIRouter(prefix="/actions", tags=["Actions"])


@router.post("/")
async def create_action(
    _: None = Depends(require_permission(Permission.ACTION_CREATE))
):
    ...


@router.get("/{action_id}")
async def get_action(
    action_id: str,
    _: None = Depends(require_permission(Permission.ACTION_READ))
):
    ...


@router.post("/{action_id}/run")
async def run_action(
    action_id: str,
    _: None = Depends(require_permission(Permission.ACTION_EXECUTE))
):
    ...


@router.post("/{action_id}/publish")
async def publish_action(
    action_id: str,
    _: None = Depends(require_permission(Permission.ACTION_PUBLISH))
):
    ...
```

### Space-Scoped Permission Check

When a permission applies only within a Space, pass the space context alongside the principal:

```python
async def require_space_permission(permission: Permission, space_id: str):
    """
    Checks if the principal holds a permission scoped to a specific space.
    """
    async def _check(request: Request) -> None:
        principal = await resolve_principal(request)

        # Resolve roles that apply to this specific space
        space_roles = principal.space_roles.get(space_id, [])

        granted: set[Permission] = set()
        for role in space_roles:
            granted.update(PERMISSION_SETS.get(role, set()))

        if permission not in granted:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied in space {space_id}: {permission.value}",
            )

    return Depends(_check)
```

---

## 14. Resource-Instance Permissions

Resource-instance permissions allow a principal to be granted access to **one specific resource** — identified by its ID — without holding a broader type-scoped role. This is the foundation of resource sharing, action-level access delegation, and fine-grained API key scoping.

---

### 14.1 Concept

A standard type-scoped permission such as `action:execute` asks:

> _"Can this principal execute actions (that they own or have space access to)?"_

An instance-scoped permission such as `action:execute:abc123` asks:

> _"Can this principal execute this specific action, regardless of who owns it or which space it belongs to?"_

Instance-scoped permissions are **additive**. They do not replace type-scoped permissions — they extend access to resources the principal would otherwise not be able to reach through role or ownership alone.

---

### 14.2 Permission String Format

```
<resource>:<action>:<resource_id>
```

| Segment | Description | Example |
|---------|-------------|---------|
| `resource` | The resource type, singular lowercase | `action`, `graph`, `file` |
| `action` | The operation being granted | `execute`, `read`, `share` |
| `resource_id` | The unique ID of the specific resource instance | `abc123`, `graph_xyz` |

**Valid examples:**

```
action:execute:abc123
action:read:abc123
action:share:abc123
action:update:abc123
graph:execute:graph_xyz
graph:read:graph_xyz
file:read:file_001
file:delete:file_001
```

**Invalid forms:**

```
action:execute:          # Missing resource_id — use type-scoped form instead
action::abc123           # Missing action — malformed
:execute:abc123          # Missing resource — malformed
```

---

### 14.3 When Instance-Scoped Permissions Are Created

Instance-scoped permissions are not defined in static permission sets (roles). They are **created dynamically** when a resource owner or admin performs one of the following operations:

| Trigger | Example |
|---------|---------|
| User shares an action with another user | User A shares `action:abc123` → grants `action:execute:abc123` to User B |
| User publishes an action to a Space | Grants `action:read:abc123` + `action:execute:abc123` to all Space members |
| Admin grants an API key scoped access | API key is issued with `action:execute:abc123` only |
| System provisions a service-to-service grant | Internal agent is granted `action:execute:abc123` to call a specific action |

---

### 14.4 Storage Model

Instance-scoped permissions must be stored in persistent storage — they cannot live only in a JWT because they are created at runtime after the token is issued.

#### Database Record: `instance_permission`

```python
class InstancePermission(BaseModel):
    id: str                    # Unique grant ID (UUID)
    principal_id: str          # Who holds this grant (user ID, API key ID, service ID)
    principal_type: str        # "user" | "api_key" | "service"
    permission: str            # Full string: "action:execute:abc123"
    resource_type: str         # "action" | "graph" | "file" (parsed from permission)
    resource_action: str       # "execute" | "read" | "share" (parsed from permission)
    resource_id: str           # "abc123" (parsed from permission)
    granted_by: str            # Principal ID of the user/admin who created this grant
    granted_at: datetime       # Timestamp of grant creation
    expires_at: datetime | None  # Optional expiry — None means no expiry
    revoked: bool              # Soft-delete flag
    revoked_at: datetime | None
    revoked_by: str | None
    note: str | None           # Optional human-readable reason for grant
```

#### Recommended Indexes

```
Index 1: (principal_id, resource_type, resource_id)   # Fast lookup per principal per resource
Index 2: (resource_id, resource_type)                  # Look up all principals with access to a resource
Index 3: (principal_id, revoked)                       # Fetch all active grants for a principal
Index 4: (expires_at)                                  # TTL expiry sweep
```

---

### 14.5 Grant and Revoke Lifecycle

```
Resource Owner / Admin
    │
    ├── POST /actions/{action_id}/share
    │       body: { principal_id, actions: ["execute", "read"] }
    │       → Creates InstancePermission records:
    │             "action:execute:abc123" for principal
    │             "action:read:abc123" for principal
    │
    ├── DELETE /actions/{action_id}/share/{principal_id}
    │       → Sets revoked=True on matching InstancePermission records
    │
    └── GET /actions/{action_id}/share
            → Lists all active InstancePermission records for this action
```

**Grant endpoint contract:**

```python
class ShareActionRequest(BaseModel):
    principal_id: str
    principal_type: str           # "user" | "api_key"
    actions: list[str]            # ["execute", "read", "share"]
    expires_at: datetime | None   # Optional expiry
    note: str | None

class ShareActionResponse(BaseModel):
    grants: list[InstancePermissionOut]  # Created grant records
```

---

### 14.6 Evaluation Logic

When the authorization layer evaluates a request, instance-scoped permissions are checked **first**, before any role-based resolution.

```python
async def has_instance_permission(
    principal_id: str,
    resource_type: str,
    resource_action: str,
    resource_id: str,
    db: AsyncIOMotorCollection,
) -> bool:
    """
    Returns True if the principal holds an active, non-expired
    instance-scoped grant for this specific resource.
    """
    now = datetime.utcnow()
    grant = await db.find_one({
        "principal_id": principal_id,
        "resource_type": resource_type,
        "resource_action": resource_action,
        "resource_id": resource_id,
        "revoked": False,
        "$or": [
            {"expires_at": None},
            {"expires_at": {"$gt": now}},
        ]
    })
    return grant is not None
```

---

### 14.7 FastAPI `Depends` for Instance-Scoped Permission

```python
# src/autobots/auth/depends.py

from fastapi import Depends, HTTPException, Request, status
from autobots.auth.permissions import Permission
from autobots.auth.instance_permissions import has_instance_permission
from autobots.auth.context import resolve_principal
from autobots.auth.permission_sets import PERMISSION_SETS


def require_instance_permission(resource_type: str, resource_action: str):
    """
    Enforces instance-scoped permission for a route that operates on
    a specific resource identified by a path parameter.

    Resolution order:
      1. Instance-scoped grant  (action:execute:<action_id>)
      2. Ownership check        (principal is the resource owner)
      3. Space-scope role       (principal has a role in the resource's space)
      4. Global role            (principal holds a platform-level permission)

    Usage:
        @router.post("/{action_id}/run")
        async def run_action(
            action_id: str,
            _: None = Depends(require_instance_permission("action", "execute"))
        ):
            ...
    """
    async def _check(
        request: Request,
        resource_id: str = None,   # Pulled from path by FastAPI if named correctly
    ) -> None:
        principal = await resolve_principal(request)

        # Extract resource_id from path parameters
        # Conventionally the path param is named after the resource type: action_id, graph_id, etc.
        path_param_key = f"{resource_type}_id"
        resource_id = request.path_params.get(path_param_key)

        if not resource_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Missing path parameter: {path_param_key}",
            )

        # --- Step 1: Instance-scoped grant check ---
        if await has_instance_permission(
            principal_id=principal.id,
            resource_type=resource_type,
            resource_action=resource_action,
            resource_id=resource_id,
            db=request.app.state.db["instance_permissions"],
        ):
            return  # ALLOW

        # --- Step 2: Ownership check ---
        resource = await request.app.state.db[f"{resource_type}s"].find_one(
            {"_id": resource_id}
        )
        if not resource:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        if resource.get("owner_id") == principal.id:
            # Owner must still hold the type-scoped permission via their role
            type_permission = f"{resource_type}:{resource_action}"
            granted = _resolve_type_permissions(principal)
            if type_permission in granted:
                return  # ALLOW

        # --- Step 3 & 4: Space-scope and global role check ---
        type_permission = f"{resource_type}:{resource_action}"
        granted = _resolve_type_permissions(principal, space_id=resource.get("space_id"))
        if type_permission in granted:
            return  # ALLOW

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Permission denied: {resource_type}:{resource_action}:{resource_id}. "
                f"You are not the owner, do not hold a space role with this permission, "
                f"and have not been granted explicit access to this resource."
            ),
        )

    return Depends(_check)


def _resolve_type_permissions(principal, space_id: str | None = None) -> set[str]:
    """Expand all role-based permissions for the principal."""
    granted: set[str] = set()

    # Global roles
    for role in principal.roles:
        granted.update(p.value for p in PERMISSION_SETS.get(role, set()))

    # Space roles (if space context is available)
    if space_id and principal.space_roles:
        for role in principal.space_roles.get(space_id, []):
            granted.update(p.value for p in PERMISSION_SETS.get(role, set()))

    return granted
```

#### Route Usage

```python
# src/autobots/action/action_router.py

from fastapi import APIRouter, Depends
from autobots.auth.depends import require_instance_permission, require_permission
from autobots.auth.permissions import Permission

router = APIRouter(prefix="/actions", tags=["Actions"])


@router.post("/{action_id}/run")
async def run_action(
    action_id: str,
    # Full resolution: instance grant → ownership → space role → global role
    _: None = Depends(require_instance_permission("action", "execute"))
):
    """
    Runs action {action_id}.
    Allowed if:
      - Principal holds action:execute:abc123 (explicit share), OR
      - Principal owns the action and holds action:execute role permission, OR
      - Principal has a space role that includes action:execute for the action's space, OR
      - Principal is platform_admin.
    """
    ...


@router.get("/{action_id}")
async def get_action(
    action_id: str,
    _: None = Depends(require_instance_permission("action", "read"))
):
    ...


@router.post("/{action_id}/share")
async def share_action(
    action_id: str,
    _: None = Depends(require_instance_permission("action", "share"))
):
    # Only the owner or someone with action:share:action_id can share further
    ...
```

---

### 14.8 Instance Permission Inheritance and Cascading

When a principal holds an instance-scoped grant, only the explicitly listed actions are granted. There is **no implicit cascading**.

| Grant held | Does it imply? |
|------------|----------------|
| `action:execute:abc123` | `action:read:abc123`? **NO** — must be granted separately. |
| `action:share:abc123` | `action:execute:abc123`? **NO** — share does not imply execute. |
| `action:execute:abc123` | `action:execute` (any action)? **NO** — instance grants do not promote to type grants. |
| `action:execute` (type) | `action:execute:abc123`? **YES** — type grant covers instances the principal owns or has space access to, but NOT instances owned by others without an explicit share. |

> **Rule:** Grant only the actions that are needed. If a consumer needs to both read and run an action, issue `action:read:<id>` and `action:execute:<id>` as two separate grants.

---

### 14.9 Expiring and Revoking Instance Grants

#### Expiry

Set `expires_at` on any instance grant that should be time-limited:

```python
ShareActionRequest(
    principal_id="user_99",
    principal_type="user",
    actions=["execute"],
    expires_at=datetime.utcnow() + timedelta(days=7),
    note="Temporary access for testing sprint",
)
```

A background job or a middleware TTL sweep should check `expires_at` and mark expired grants as `revoked=True`.

#### Revocation

```python
async def revoke_instance_permission(
    grant_id: str,
    revoked_by: str,
    db: AsyncIOMotorCollection,
) -> None:
    await db.update_one(
        {"_id": grant_id},
        {"$set": {
            "revoked": True,
            "revoked_at": datetime.utcnow(),
            "revoked_by": revoked_by,
        }}
    )
```

> **Important:** Revocation must take effect immediately. If instance permissions are cached (e.g., in Redis), the cache entry for this grant must be invalidated on revocation. A stale `ALLOW` after revocation is a security defect.

---

### 14.10 Listing Who Has Access to a Resource

Resource owners and space admins should be able to list all active grants on a resource:

```python
@router.get("/{action_id}/access")
async def list_action_access(
    action_id: str,
    _: None = Depends(require_instance_permission("action", "share"))
) -> list[InstancePermissionOut]:
    """
    Returns all active instance-scoped grants for this action.
    """
    grants = await db.find({
        "resource_type": "action",
        "resource_id": action_id,
        "revoked": False,
    }).to_list()
    return [InstancePermissionOut(**g) for g in grants]
```

**Response example:**

```json
[
  {
    "id": "grant_001",
    "principal_id": "user_99",
    "principal_type": "user",
    "permission": "action:execute:abc123",
    "granted_by": "user_42",
    "granted_at": "2025-01-10T10:00:00Z",
    "expires_at": "2025-01-17T10:00:00Z",
    "note": "Temporary access for testing sprint"
  },
  {
    "id": "grant_002",
    "principal_id": "user_77",
    "principal_type": "user",
    "permission": "action:read:abc123",
    "granted_by": "user_42",
    "granted_at": "2025-01-08T08:00:00Z",
    "expires_at": null,
    "note": null
  }
]
```

---

### 14.11 Caching Instance Permissions

Instance permissions are dynamic — they are created and revoked at runtime. Caching them requires careful invalidation.

| Strategy | Recommendation |
|----------|---------------|
| **Cache key** | `instance_perm:{principal_id}:{resource_type}:{resource_action}:{resource_id}` |
| **Cache TTL** | Short — 30 to 60 seconds maximum. |
| **Cache invalidation** | On every grant creation and revocation, delete the affected cache key immediately. |
| **Cache backend** | Redis is recommended. In-process caching is not safe for distributed deployments. |
| **Negative caching** | Cache `NOT_FOUND` results for up to 10 seconds to reduce DB load on repeated denied requests, but no longer. |

---

### 14.12 Summary — Type-Scoped vs Instance-Scoped Permissions

| Dimension | Type-Scoped (`action:execute`) | Instance-Scoped (`action:execute:abc123`) |
|-----------|-------------------------------|-------------------------------------------|
| **Scope** | All accessible resources of a type | One specific resource |
| **Who assigns it** | Role system (static config) | Owner or admin at runtime |
| **Where stored** | Permission set registry (code/config) | Database (`instance_permission` collection) |
| **Inheritance** | Via role hierarchy | None — explicit per action only |
| **Expiry support** | No (role-level) | Yes (per grant) |
| **Revocable at runtime** | No (requires role reassignment) | Yes (immediate revocation) |
| **Typical use case** | Standard user capabilities | Sharing, delegation, API key scoping |
| **Evaluation order** | After instance check fails | First — checked before role resolution |

---

## 15. Design Principles and Tradeoffs

### Principle 1 — Least Privilege by Default

Every new resource and route starts with no permissions granted. Access must be explicitly added. Never default to permissive access.

### Principle 2 — Flat Permission Strings

Permissions are flat strings (`action:execute`), not nested objects. This makes them easy to store in a DB, cache in a token, log in audit trails, and compare at runtime.

### Principle 3 — Separate Role Assignment from Permission Definition

What a role *is allowed to do* (the permission set) is defined in code and versioned. Who *has* a role is stored in the database. Never mix these two concerns.

### Principle 4 — Scope Is Not Optional

Every permission check must include scope context. A permission without scope is underspecified and will either over-grant or under-grant access.

### Principle 5 — Deny by Default, Allow by Exception

The permission evaluator should return `DENY` unless a specific grant is found. Never invert this logic.

### Known Tradeoffs

| Tradeoff | Decision |
|----------|----------|
| RBAC vs ABAC | Use RBAC (role-based) for the standard layer due to simplicity. Extend with ABAC (attribute-based) only for resource-instance sharing. |
| Flat vs hierarchical roles | Flat roles with explicit inheritance sets are easier to reason about and audit than deep role hierarchies. |
| Permissions in JWT vs DB lookup | Short-lived JWTs with embedded roles are fast but stale. DB lookups are always fresh but slow. Use a short-lived cache (< 60 seconds TTL). |
| Granularity vs  maintainability | Permissions that are too coarse lose flexibility. Permissions that are too fine become unmanageable. The `<resource>:<action>` grain is the recommended balance. |

---

## 16. Common Pitfalls

| Pitfall | Description | Mitigation |
|---------|-------------|------------|
| **Permission drift** | Roles accumulate permissions over time without review. | Audit permission sets quarterly. Remove unused grants. |
| **Hardcoded role checks** | Code uses `if user.role == "admin"` instead of checking a permission. | Always check permissions, never roles, in business logic. |
| **Missing scope on permission checks** | Permission is checked without verifying resource ownership or space context. | Every `is_allowed()` call must include scope resolution. |
| **Silent DENY as 200** | An unauthorized access attempt returns an empty list instead of `403`. | Never silently hide unauthorized access. Return explicit error codes. |
| **Superuser in production** | A service account with `*:*` is used for regular user operations. | Superuser must only exist for internal service-to-service calls. |
| **No audit of permission changes** | Changes to role assignments are not logged. | Log every permission grant and revoke with principal, timestamp, and reason. |
| **Wildcard evaluation at runtime** | `*:*` is evaluated lazily during a check rather than being expanded at load time. | Expand wildcards to concrete permission lists at startup. |
| **Stale instance-permission cache** | A revoked instance grant is still cached as `ALLOW`, allowing continued unauthorized access. | Invalidate the Redis cache key immediately on grant revocation. Never use long TTLs for instance grants. |
| **Instance grant without expiry** | Instance grants are issued with no `expires_at`, creating indefinite access that is never cleaned up. | Set explicit expiry for any time-limited delegation. Run a periodic sweep to surface never-expiring grants for review. |
| **Implicit action promotion** | Code assumes `action:execute:abc123` implies `action:read:abc123`. | Instance grants are non-cascading. Enumerate every required action explicitly when creating a share grant. |
| **Skipping instance check in type-scoped routes** | A route uses `require_permission(Permission.ACTION_EXECUTE)` (type-scoped only), which ignores instance grants. Users who were given a specific share are denied. | Always use `require_instance_permission()` on resource-specific routes. Reserve type-scoped `require_permission()` only for collection-level routes such as `GET /actions`. |
| **Deleting a resource without revoking grants** | A resource is deleted but its instance grants remain in the database, pointing to a non-existent resource. | On resource deletion, cascade-delete or revoke all instance grants for that resource ID. |
| **Granting `action:share` without ownership check** | Any user who holds `action:share:abc123` can share the action further, including to principals with higher privilege. | On a share grant, verify the granting principal is either the resource owner or a space admin. Prevent share grants from escalating beyond the grantor’s own permission level. |

---

*End of Document*
