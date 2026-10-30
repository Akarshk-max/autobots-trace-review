# Space Feature — Technical Specification

## Overview

Space introduces an optional outer-level isolation boundary in the Autobots platform — analogous to the "User / Workspace" toggle in VS Code settings. Today, all isolation is at the **User** level. Space adds a **Workspace** level on top.

```
┌─────────────────────────────────┐
│           Space (optional)      │   ← Workspace-level isolation
│  ┌──────────┐  ┌──────────┐    │
│  │  User A  │  │  User B  │    │   ← User-level isolation (always on)
│  │ actions  │  │ actions  │    │
│  │ graphs   │  │ graphs   │    │
│  └──────────┘  └──────────┘    │
└─────────────────────────────────┘
```

**Key principle:** `space_id` is **optional** across all APIs and models. When absent, the system behaves exactly as today — pure user isolation. When present, it adds Space-level scoping around the user's resources. User isolation is **never weakened** — it is always enforced within a Space.

---

## Two Isolation Levels

| Level | Scope | Required | Behavior |
|-------|-------|----------|----------|
| **User** | `user_id` on every resource | Always | Every query is scoped by `user_id`. This is the existing behavior and remains unchanged. |
| **Space** | `space_id` on resources | Optional | When provided, queries are additionally scoped by `space_id`. Resources in Space A are invisible from Space B. |

**Without `space_id`:** API behaves exactly as today. All existing clients, integrations, and tests continue to work with zero changes.

**With `space_id`:** Resources are created within that Space. Queries filter by both `user_id` AND `space_id`. Sharing and collaboration happen within the Space boundary.

---

## Core Concepts

### Space

A Space is an optional organizational boundary. It groups users and their resources into an isolated environment.

- Each Space has a unique identifier (`space_id`).
- A Space has a name, description, and metadata.
- A Space is created by a user, who automatically becomes its **Owner**.

### Space Membership & Roles

| Role     | Description |
|----------|-------------|
| **Owner**  | Full control — manage Space, manage members, access all resources. A Space must always have at least one Owner. |
| **Admin**  | Can manage members (invite, remove, promote to Admin) and access all resources. Cannot delete the Space or remove an Owner. |
| **Member** | Can create and manage their own resources. Can view resources shared with the Space. Cannot manage other members. |

A user can belong to multiple Spaces. Each membership is independent.

### Resource Behavior Within a Space

User isolation is always preserved:
- A Member's private resources are **only visible to that Member**, even within the same Space.
- Resources can be explicitly **shared with the Space** by their creator, making them visible to all Space members.
- Owner/Admin of the Space can see all resources (admin override).

---

## Incremental Implementation Plan

6 self-contained increments. Each is fully functional and builds on the previous.

---

### Increment 1: Space CRUD — Create and Manage Spaces

**Goal:** Users can create, read, update, and delete Spaces. The creator becomes the Owner. This is the foundation.

**What gets built:**

```
src/autobots/space/
    space_doc_model.py     # SpaceDoc, SpaceCreate, SpaceFind, SpaceUpdate, SpaceMemberRole enum
    space_crud.py          # SpaceCRUD (extends CRUDBase)
    space.py               # Core logic (create_space auto-adds owner membership)
    user_space.py          # UserSpaces — scopes all ops by user_id
    space_service.py       # Public contract for other modules

src/autobots/api/v1/api_space/
    api_space.py           # REST endpoints
```

**Data models:**

```python
class SpaceMemberRole(str, Enum):
    owner = "owner"
    admin = "admin"
    member = "member"

# --- Space document ---
class SpaceCreate(BaseModel):
    name: str
    description: str = ""

class SpaceDocCreate(UserId, SpaceCreate, CreatedAt, UpdatedAt):
    pass

class SpaceDoc(SpaceDocCreate):
    __collection__ = "Spaces"
    id: str = Field(..., alias="_id")

# --- Space Membership document ---
class SpaceMemberCreate(BaseModel):
    space_id: str
    user_id: str
    role: SpaceMemberRole

class SpaceMemberDocCreate(SpaceMemberCreate, CreatedAt, UpdatedAt):
    invited_by: str  # user_id of the inviter (self for creator)

class SpaceMemberDoc(SpaceMemberDocCreate):
    __collection__ = "SpaceMembers"
    id: str = Field(..., alias="_id")
```

**API endpoints:**

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST   | `/v1/spaces` | Any user | Create Space (caller becomes Owner) |
| GET    | `/v1/spaces` | Any user | List Spaces the user belongs to |
| GET    | `/v1/spaces/{space_id}` | Member+ | Get Space details |
| PUT    | `/v1/spaces/{space_id}` | Owner | Update name/description |
| DELETE | `/v1/spaces/{space_id}` | Owner | Delete Space |

**Key behaviors:**
- `create_space` inserts a `SpaceDoc` and a `SpaceMemberDoc` (role=owner) atomically.
- `list_spaces` queries `SpaceMembers` by `user_id`, then fetches matching `Spaces`.
- `delete_space` deletes the Space doc and all its membership docs.
- A Space must always have at least one Owner.

**MongoDB collections created:** `Spaces`, `SpaceMembers`

**Tests:** CRUD operations, owner auto-creation, list-only-my-spaces, only-owner-can-delete.

**Complete when:** A user can create a Space, see it listed, update it, and delete it.

---

### Increment 2: Membership Management — Roles and Member CRUD

**Goal:** Space Owners and Admins can add/remove members and change roles. Members can leave.

**What gets built:**

```
src/autobots/space/
    space_auth.py          # FastAPI dependencies for Space membership validation
```

Extends: `api_space.py` with membership endpoints, `user_space.py` with membership methods.

**API endpoints:**

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST   | `/v1/spaces/{space_id}/members` | Owner/Admin | Add a member (by user_id or email, with role) |
| GET    | `/v1/spaces/{space_id}/members` | Member+ | List all members |
| PUT    | `/v1/spaces/{space_id}/members/{user_id}` | Owner | Change member role |
| DELETE | `/v1/spaces/{space_id}/members/{user_id}` | Owner/Admin or self | Remove member (or leave) |

**Authorization matrix:**

```
                          Owner    Admin    Member
Add member                 ✓        ✓        ✗
Remove member              ✓        ✓*       ✗ (self only)
Change role                ✓        ✗        ✗
Delete Space               ✓        ✗        ✗
View members               ✓        ✓        ✓

* Admin cannot remove an Owner
```

**FastAPI dependencies (`space_auth.py`):**

```python
async def get_space_member(space_id, user, db) -> SpaceMemberDoc:
    """Validates user is a member of the space. Returns membership. 403 if not."""

async def require_owner(member) -> SpaceMemberDoc:
    """Asserts role == owner. 403 otherwise."""

async def require_admin_or_owner(member) -> SpaceMemberDoc:
    """Asserts role in (owner, admin). 403 otherwise."""
```

**Tests:** Role enforcement, cannot-remove-last-owner, self-leave, admin-cannot-touch-owner.

**Complete when:** Owners can manage a team of users within a Space with proper role enforcement.

---

### Increment 3: Invitation Flow

**Goal:** Add an invitation workflow alongside direct-add. Users receive invitations they can accept or decline.

**What gets built:**

```
src/autobots/space/
    space_invitation_doc_model.py   # InvitationDoc models
    space_invitation_crud.py        # InvitationCRUD

src/autobots/api/v1/api_space/
    api_space_invitation.py         # Invitation endpoints
```

**Data model:**

```python
class InvitationStatus(str, Enum):
    pending = "pending"
    accepted = "accepted"
    declined = "declined"
    expired = "expired"

class SpaceInvitationDoc:
    __collection__ = "SpaceInvitations"
    id: str
    space_id: str
    invitee_email: str
    role: SpaceMemberRole
    invited_by: str          # user_id
    status: InvitationStatus
    created_at: datetime
    expires_at: datetime     # e.g., 7 days from creation
```

**API endpoints:**

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST   | `/v1/spaces/{space_id}/invitations` | Owner/Admin | Send invitation |
| GET    | `/v1/spaces/{space_id}/invitations` | Owner/Admin | List invitations for a Space |
| DELETE | `/v1/spaces/{space_id}/invitations/{invitation_id}` | Owner/Admin | Revoke pending invitation |
| GET    | `/v1/invitations` | Any user | List my pending invitations |
| POST   | `/v1/invitations/{invitation_id}/accept` | Invitee | Accept → creates SpaceMemberDoc |
| POST   | `/v1/invitations/{invitation_id}/decline` | Invitee | Decline |

**MongoDB collection created:** `SpaceInvitations`

**Complete when:** Full invitation lifecycle works alongside direct-add from Increment 2.

---

### Increment 4: Space-Scoped Resources — Optional `space_id` on All Resources

**Goal:** Every resource can optionally belong to a Space. When `space_id` is provided, queries scope by it. When absent, behavior is unchanged from today.

**This is the critical increment.** It wires Space isolation into the existing resource system without breaking anything.

**4a. Model changes — `space_id` is Optional:**

```python
# New mixin
class SpaceId(BaseModel):
    space_id: Optional[str] = None

# Applied to each resource's DocCreate:
class ActionDocCreate(UserId, SpaceId, ActionCreate, CreatedAt, UpdatedAt): ...
class ActionGraphDocCreate(UserId, SpaceId, ActionGraphCreate, CreatedAt, UpdatedAt): ...
# ... etc for all resources
```

`space_id` defaults to `None`. Existing documents without `space_id` continue to work — they are "User-level" resources with no Space affiliation.

**4b. DocFind models — Optional filter:**

```python
class ActionDocFind(ActionFind):
    user_id: str | None = None
    space_id: str | None = None  # When set, filters by space
```

**4c. CRUD behavior — additive filtering:**

```python
# In CRUDBase or user_* wrapper:
filter = {"user_id": self.user_id}
if self.space_id:
    filter["space_id"] = self.space_id
# else: no space filter, pure user isolation (today's behavior)
```

**4d. User wrapper changes:**

`space_id` becomes an optional init parameter:

```python
class UserActions:
    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase, space_id: str | None = None):
        self.user_id = str(user.id)
        self.space_id = space_id  # None = user-level, set = space-level
```

When `space_id` is set, all DocCreate/DocFind/DocUpdate objects get `space_id` stamped.

**4e. API changes — `space_id` as optional query parameter:**

Existing routes gain an optional `space_id` query parameter. No new routes, no URL changes.

```python
# Same route, optional space_id query param
GET  /v1/actions                       # no space_id → user-level (today's behavior)
GET  /v1/actions?space_id=xxx          # with space_id → space-scoped

POST /v1/actions                       # create without space (user-level)
POST /v1/actions?space_id=xxx          # create within a space
```

A single shared FastAPI dependency handles validation:

```python
async def get_optional_space_id(
    space_id: str | None = Query(default=None),
    user: UserORM = Depends(get_user_from_access_token),
    db = Depends(get_mongo_db),
) -> str | None:
    """If space_id is provided, validates membership. Returns space_id or None."""
    if space_id:
        await validate_space_membership(space_id, user, db)  # 403 if not a member
    return space_id
```

This dependency is added to all resource routers. Endpoints pass the resolved `space_id` to `UserActions(user, db, space_id=space_id)`.

**Why query param over nested routes:**
- Matches the optional semantics — `space_id` is truly optional, not a different URL.
- No route duplication — one set of routes, not two.
- Simpler frontend integration — clients just append `?space_id=xxx` to existing calls.
- All existing tests, clients, and integrations work without any URL changes.

**4f. No migration needed:**

Because `space_id` is optional and defaults to `None`, existing data works as-is. Resources without `space_id` are "User-level" — visible via `/v1/actions` (no query param). Resources with `space_id` are "Space-level" — visible via `/v1/actions?space_id=xxx`.

**Resources affected:**

| Resource | DocCreate | User Wrapper | API Router |
|----------|-----------|--------------|------------|
| Action | `ActionDocCreate` | `UserActions` | `api_action.py` |
| ActionGraph | `ActionGraphDocCreate` | `UserActionGraphs` | `api_action_graphs.py` |
| ActionTeam | `ActionTeamDocCreate` | `UserActionTeams` | `api_action_teams.py` |
| ActionResult | `ActionResultDocCreate` | `UserActionResult` | `api_action_results.py` |
| ActionGraphResult | `ActionGraphResultDocCreate` | `UserActionGraphResults` | `api_action_graph_results.py` |
| ActionTeamResult | `ActionTeamResultDocCreate` | `UserActionTeamResults` | `api_action_team_results.py` |
| AppCreds | `AppCredsDocCreate` | `UserAppCreds` | `api_app_creds.py` |
| AppAuth | `IntegrationDocCreate` | `UserIntegration` | `api_app_auth.py` |
| Datastore | `DatastoreMetaDocCreate` | `UserDatastores` | `api_datastore.py` |
| VectorStore | `VectorstoreMetaDocCreate` | `UserVectorstores` | `api_vectorstore.py` |
| Schedules | `ScheduleDocCreate` | `UserSchedules` | `api_schedules.py` |
| Webhooks | `WebhookDocCreate` | `UserWebhooks` | `api_webhooks.py` |
| Skills | `SkillDocCreate` | `UserSkills` | `api_skills.py` |
| Characters | `CharacterDocCreate` | `UserCharacters` | `api_characters.py` |

**Tests:** All existing tests pass unchanged (no `space_id` = user-level). New tests for space-scoped routes and cross-space isolation.

**Complete when:** Resources can optionally be created in a Space. Space-scoped queries work. All existing behavior is preserved.

---

### Increment 5: Resource Sharing — Unified `shared_with` for Users and Spaces

**Goal:** Extend the existing `shared_with` field to support both email-based sharing (existing) and space-based sharing. No new `visibility` field — one mechanism for all sharing.

**Design: Unified `shared_with` with typed entries**

The existing `shared_with` field holds email addresses as strings. We extend it to also accept `SpaceId` objects using a union type:

```python
from src.autobots.data_model.space_id import SpaceId

shared_with: List[str | SpaceId] = []
# Can contain:
#   "user@example.com"              — share with a specific user (str)
#   SpaceId(space_id="sp_abc123")   — share with all members of a space
#
# JSON wire format / MongoDB storage:
#   ["alice@example.com", {"space_id": "sp_abc123"}]
```

**No new fields, no prefix conventions.** The `shared_with` field already exists on Action, ActionGraph, ActionTeam. Pydantic discriminates `str` vs `SpaceId` (object) natively — no validators needed.

**Why not a separate `visibility` field:**
- One field, one concept — no two fields to keep in sync.
- More granular — share with specific spaces, specific users, or both.
- Backward compatible — existing email-based sharing keeps working unchanged.
- Type-safe — `str | SpaceId` is self-documenting, no string prefix parsing needed.

**Query logic:**

```python
# Email sharing query (unchanged):
{"shared_with": "alice@example.com"}

# Space sharing query (dot notation into array elements):
{"shared_with.space_id": "sp_abc123"}

# Combined scope filter (list/search methods):
filter = {
    "$or": [
        {"user_id": self.user_id},                     # my own resources
        {"shared_with": self.user.email},               # shared with me by email
        {"shared_with.space_id": space_id},             # shared with this space
    ]
}
```

MongoDB's dot notation on arrays matches embedded documents within the array. The `CRUDBase._build_find_params` method handles this automatically — when `shared_with` is a `SpaceId` object, `model_dump()` produces a dict which gets flattened to dot notation.

**API:** Uses existing CRUD endpoints. Clients send `shared_with` in create/update payloads:
```json
{"shared_with": ["alice@example.com", {"space_id": "sp_abc123"}]}
```

**Affected resources:** Action, ActionGraph, ActionTeam — all Find, Create, Update, Doc, and LiteDoc models updated.

**Helper utilities** in `src/autobots/space/space_sharing.py`:
- `get_emails_from_shared_with(shared_with)` — extract email strings
- `get_space_ids_from_shared_with(shared_with)` — extract space IDs from SpaceId objects

**Key behaviors:**
- Only the resource creator can modify `shared_with`.
- Shared resources are read-only to non-owners (view/use, not edit/delete).
- Owner/Admin of the Space can see all resources in their space regardless of `shared_with`.
- Config scrubbing on shared Actions (existing pattern — `config = {}`).
- `is_published` remains unchanged (system-level resources only).
- `space_id` flows as explicit parameter through the call chain — API endpoints pass it to service methods, which include it in both `find_params` and all `or_find_queries` for consistent scoping.

**Helper utilities** in `src/autobots/space/space_sharing.py`:

```python
from src.autobots.data_model.space_id import SpaceId

def get_space_ids_from_shared_with(shared_with: list[str | SpaceId] | None) -> list[str]:
    if not shared_with:
        return []
    return [entry.space_id for entry in shared_with if isinstance(entry, SpaceId) and entry.space_id]

def get_emails_from_shared_with(shared_with: list[str | SpaceId] | None) -> list[str]:
    if not shared_with:
        return []
    return [entry for entry in shared_with if isinstance(entry, str)]
```

**Complete when:** Resources can be shared with specific users (by email) and/or entire spaces (by `SpaceId`) using the unified `shared_with` field. List endpoints return owned + email-shared + space-shared resources correctly. `space_id` is included in all query params for consistent scoping.

---

### Increment 6: Default Space & Convenience

**Goal:** Optional quality-of-life features — auto-create a Space for users who want one, provide a "move to Space" operation for existing resources.

**What gets built:**

```
src/autobots/space/
    space_default.py       # Optional default space auto-creation
```

**Features:**
- **Create default Space:** When a user creates their first Space, existing user-level resources can optionally be moved into it.
- **Move resource to Space:** API to set `space_id` on an existing user-level resource, moving it into a Space.
- **Remove from Space:** API to clear `space_id`, moving a resource back to user-level.

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST   | `/v1/actions/{action_id}/adopt?space_id=xxx` | Resource owner + Space member | Move a user-level resource into a Space |
| POST   | `/v1/actions/{action_id}/detach` | Resource owner | Remove resource from its Space (back to user-level) |

(Same pattern per resource type.)

**No forced migration.** Users who never use Spaces see no difference. Users who adopt Spaces can gradually move resources in.

**Complete when:** Users can move existing resources into and out of Spaces.

---

## Increment Dependency Graph

```
Increment 1 (Space CRUD)
    │
    ├──→ Increment 2 (Membership)
    │        │
    │        ├──→ Increment 3 (Invitations)
    │        │
    │        └──→ Increment 4 (Space-Scoped Resources)  ← critical
    │                 │
    │                 ├──→ Increment 5 (Visibility)
    │                 │
    │                 └──→ Increment 6 (Default Space & Adopt)
    │
    └─── All existing APIs continue to work unchanged throughout
```

**Critical path:** 1 → 2 → 4. Increments 3, 5, 6 are independent of each other.

## Design Principles

1. **`space_id` is always optional.** No existing API breaks. No migration required.
2. **User isolation is never weakened.** `user_id` scoping is always enforced, with or without Space.
3. **Additive, not destructive.** Space adds a layer; it doesn't replace the user layer.
4. **Two worlds coexist.** User-level resources (no `space_id`) and Space-level resources (`space_id` set) live side by side in the same collections.
5. **`space_id` flows as optional query parameter** — validated by a shared FastAPI dependency, passed explicitly through the call chain. Not via Context, not via headers.
