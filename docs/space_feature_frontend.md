# Space Feature — Frontend Integration Guide

**Target project:** `autobots-surface` (Next.js)
**Backend branch:** `AUT-1317-space`

This document describes the backend APIs available for the Space (workspace) feature and how to integrate them into the `autobots-surface` frontend.

---

## Table of Contents

1. [Backend API Reference](#1-backend-api-reference)
2. [Space Context — `space_id` Query Parameter](#2-space-context--space_id-query-parameter)
3. [API Client Layer](#3-api-client-layer)
4. [Space State Management](#4-space-state-management)
5. [Hook Layer](#5-hook-layer)
6. [Shell / Navigation Changes](#6-shell--navigation-changes)
7. [Pages and Components](#7-pages-and-components)
8. [Resource Sharing (`shared_with`)](#8-resource-sharing-shared_with)
9. [Implementation Order](#9-implementation-order)

---

## 1. Backend API Reference

### 1.1 Space CRUD

Base path: `/v1/spaces`

| Method | Endpoint | Request Body | Response | Auth |
|--------|----------|-------------|----------|------|
| POST | `/spaces/` | `{ name, description? }` | `SpaceDoc` | Bearer token |
| GET | `/spaces/` | — | `SpaceDocsFound` | Bearer token |
| GET | `/spaces/{space_id}` | — | `SpaceDoc` | Bearer token |
| PUT | `/spaces/{space_id}` | `{ name?, description? }` | `SpaceDoc` | Bearer token |
| DELETE | `/spaces/{space_id}` | — | `204` | Bearer token |

**Response types:**

```typescript
interface SpaceDoc {
  _id: string;
  name: string;
  description: string;
  user_id: string;
  created_at: string;
  updated_at: string | null;
}

interface SpaceDocsFound {
  docs: SpaceLiteDoc[];
  total_count: number;
  limit: number;
  offset: number;
}
```

### 1.2 Membership Management

Base path: `/v1/spaces/{space_id}/members`

| Method | Endpoint | Request Body | Response | Auth |
|--------|----------|-------------|----------|------|
| POST | `/spaces/{space_id}/members` | `{ user_id, role? }` | `SpaceMemberDoc` | Admin/Owner |
| GET | `/spaces/{space_id}/members` | — | `SpaceMemberDocsFound` | Member |
| PUT | `/spaces/{space_id}/members/{target_user_id}` | `{ role }` | `SpaceMemberDoc` | Owner |
| DELETE | `/spaces/{space_id}/members/{target_user_id}` | — | `204` | Member (self) or Admin/Owner |

**Roles:** `owner`, `admin`, `member`

```typescript
interface SpaceMemberDoc {
  _id: string;
  space_id: string;
  user_id: string;
  role: "owner" | "admin" | "member";
  invited_by: string;
  created_at: string;
  updated_at: string | null;
}
```

### 1.3 Invitation Flow

| Method | Endpoint | Request Body | Response | Auth |
|--------|----------|-------------|----------|------|
| POST | `/spaces/{space_id}/invitations` | `{ invitee_email, role? }` | `SpaceInvitationDoc` | Admin/Owner of space |
| GET | `/spaces/{space_id}/invitations` | — | `SpaceInvitationDocsFound` | Admin/Owner of space |
| DELETE | `/spaces/{space_id}/invitations/{invitation_id}` | — | `204` | Admin/Owner of space |
| GET | `/invitations/` | — | `SpaceInvitationDocsFound` | Bearer token (lists user's received invitations) |
| POST | `/invitations/{invitation_id}/accept` | — | `SpaceMemberDoc` | Bearer token (invitee) |
| POST | `/invitations/{invitation_id}/decline` | — | `204` | Bearer token (invitee) |

```typescript
interface SpaceInvitationDoc {
  _id: string;
  space_id: string;
  invitee_email: string;
  role: "owner" | "admin" | "member";
  invited_by: string;
  status: "pending" | "accepted" | "declined" | "expired";
  created_at: string;
  updated_at: string | null;
  expires_at: string;
}
```

**Invitation expiry:** 7 days from creation.

### 1.4 Space-Scoped Resources

All existing resource endpoints (actions, graphs, teams) accept an optional `space_id` query parameter. When provided, the backend:
1. Validates the caller is a member of the space (403 if not)
2. Scopes all CRUD operations to that space
3. Includes resources shared with the space via `shared_with`

No endpoint signatures change — only a query parameter is added.

**System / published resources:** System actions (`is_published=True`, owned by the system user) are global — available to all users across all workspaces. The frontend does **not** need special handling for these. The backend resolves resources in order: owned → shared → system, and the system fallback always ignores `space_id`. So the frontend can safely send `space_id` on every request when in a space context — system resources will still be found.

---

## 2. Space Context — `space_id` Query Parameter

The backend uses a **query parameter** (not a header) to determine workspace context:

```
GET /v1/actions/?space_id=sp_abc123&limit=30
GET /v1/actions/act_xyz?space_id=sp_abc123
POST /v1/actions/?space_id=sp_abc123
GET /v1/action_graphs/?space_id=sp_abc123
GET /v1/action_teams/?space_id=sp_abc123
```

**Rule:** When the user has selected a space in the UI, every API call to resource endpoints must include `?space_id=<selected_space_id>`. When no space is selected (personal context), omit the parameter.

The API client should append this automatically based on the current space selection.

---

## 3. API Client Layer

### 3.1 New file: `src/lib/api/spaces.ts`

Create a new API module following the existing pattern in `teams.ts`, `actions.ts`:

```typescript
import { api } from "./client";

// Types
export interface SpaceDoc {
  _id: string;
  name: string;
  description: string;
  user_id: string;
  created_at: string;
  updated_at: string | null;
}

export interface SpaceLiteDoc {
  _id: string;
  name: string;
  description: string;
  user_id: string;
  created_at: string;
  updated_at: string | null;
}

export interface SpaceDocsFound {
  docs: SpaceLiteDoc[];
  total_count: number;
  limit: number;
  offset: number;
}

export type SpaceMemberRole = "owner" | "admin" | "member";

export interface SpaceMemberDoc {
  _id: string;
  space_id: string;
  user_id: string;
  role: SpaceMemberRole;
  invited_by: string;
  created_at: string;
  updated_at: string | null;
}

export interface SpaceMemberDocsFound {
  docs: SpaceMemberDoc[];
  total_count: number;
  limit: number;
  offset: number;
}

export type InvitationStatus = "pending" | "accepted" | "declined" | "expired";

export interface SpaceInvitationDoc {
  _id: string;
  space_id: string;
  invitee_email: string;
  role: SpaceMemberRole;
  invited_by: string;
  status: InvitationStatus;
  created_at: string;
  updated_at: string | null;
  expires_at: string;
}

export interface SpaceInvitationDocsFound {
  docs: SpaceInvitationDoc[];
  total_count: number;
  limit: number;
  offset: number;
}

export const spacesApi = {
  // --- Space CRUD ---
  create(data: { name: string; description?: string }) {
    return api.post<SpaceDoc>("/spaces/", data);
  },
  list(params?: { limit?: number; offset?: number }) {
    const p: Record<string, string> = {};
    if (params?.limit != null) p.limit = String(params.limit);
    if (params?.offset != null) p.offset = String(params.offset);
    return api.get<SpaceDocsFound>("/spaces/", p);
  },
  get(spaceId: string) {
    return api.get<SpaceDoc>(`/spaces/${spaceId}`);
  },
  update(spaceId: string, data: { name?: string; description?: string }) {
    return api.put<SpaceDoc>(`/spaces/${spaceId}`, data);
  },
  delete(spaceId: string) {
    return api.delete<void>(`/spaces/${spaceId}`);
  },

  // --- Members ---
  addMember(spaceId: string, data: { user_id: string; role?: SpaceMemberRole }) {
    return api.post<SpaceMemberDoc>(`/spaces/${spaceId}/members`, data);
  },
  listMembers(spaceId: string, params?: { limit?: number; offset?: number }) {
    const p: Record<string, string> = {};
    if (params?.limit != null) p.limit = String(params.limit);
    if (params?.offset != null) p.offset = String(params.offset);
    return api.get<SpaceMemberDocsFound>(`/spaces/${spaceId}/members`, p);
  },
  updateMemberRole(spaceId: string, targetUserId: string, data: { role: SpaceMemberRole }) {
    return api.put<SpaceMemberDoc>(`/spaces/${spaceId}/members/${targetUserId}`, data);
  },
  removeMember(spaceId: string, targetUserId: string) {
    return api.delete<void>(`/spaces/${spaceId}/members/${targetUserId}`);
  },

  // --- Invitations (space-scoped) ---
  sendInvitation(spaceId: string, data: { invitee_email: string; role?: SpaceMemberRole }) {
    return api.post<SpaceInvitationDoc>(`/spaces/${spaceId}/invitations`, data);
  },
  listInvitations(spaceId: string, params?: { limit?: number; offset?: number }) {
    const p: Record<string, string> = {};
    if (params?.limit != null) p.limit = String(params.limit);
    if (params?.offset != null) p.offset = String(params.offset);
    return api.get<SpaceInvitationDocsFound>(`/spaces/${spaceId}/invitations`, p);
  },
  revokeInvitation(spaceId: string, invitationId: string) {
    return api.delete<void>(`/spaces/${spaceId}/invitations/${invitationId}`);
  },

  // --- Invitations (user-scoped) ---
  listMyInvitations(params?: { limit?: number; offset?: number }) {
    const p: Record<string, string> = {};
    if (params?.limit != null) p.limit = String(params.limit);
    if (params?.offset != null) p.offset = String(params.offset);
    return api.get<SpaceInvitationDocsFound>("/invitations/", p);
  },
  acceptInvitation(invitationId: string) {
    return api.post<SpaceMemberDoc>(`/invitations/${invitationId}/accept`);
  },
  declineInvitation(invitationId: string) {
    return api.post<void>(`/invitations/${invitationId}/decline`);
  },
};
```

### 3.2 Export from `src/lib/api/index.ts`

Add the new module to the barrel export:

```typescript
export { spacesApi } from "./spaces";
```

### 3.3 Modify `AutobotsClient` to inject `space_id`

The API client (`src/lib/api/client.ts`) needs to automatically append `space_id` to resource requests when a space is active. Recommended approach:

```typescript
// In AutobotsClient, add a space_id getter
private getSpaceId(): string | null {
  // Read from the space store (see Section 4)
  if (typeof window === "undefined") return null;
  return localStorage.getItem("kiwi-active-space") || null;
}

// Modify the get/post/put/delete methods to inject space_id
async get<T>(path: string, params?: Record<string, string>): Promise<T> {
  const headers = await this.getAuthHeaders();
  const url = new URL(`${this.baseUrl}${path}`);
  if (params) {
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
  }
  // Auto-inject space_id for resource endpoints
  const spaceId = this.getSpaceId();
  if (spaceId && this.isResourcePath(path)) {
    url.searchParams.set("space_id", spaceId);
  }
  const res = await fetch(url.toString(), { headers });
  if (!res.ok) throw new ApiError(res.status, `GET ${path} failed: ${res.statusText}`);
  return res.json();
}

private isResourcePath(path: string): boolean {
  // Resource endpoints that support space scoping.
  // No need to exclude system endpoints — the backend handles
  // system/published resources correctly even when space_id is sent.
  return /^\/(actions|action_graphs|action_teams)/.test(path);
}
```

Apply the same pattern to `post`, `put`, `delete` methods.

---

## 4. Space State Management

### 4.1 New file: `src/lib/space-store.ts`

Follow the same pattern as `account-store.ts` — a lightweight localStorage-backed store:

```typescript
const ACTIVE_SPACE_KEY = "kiwi-active-space";
const SPACE_CACHE_KEY = "kiwi-space-cache";

export interface CachedSpace {
  _id: string;
  name: string;
  role: SpaceMemberRole;  // caller's role in this space
}

/** Get the currently active space ID, or null for personal context. */
export function getActiveSpaceId(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(ACTIVE_SPACE_KEY) || null;
}

/** Set the active space. Pass null to switch to personal context. */
export function setActiveSpaceId(spaceId: string | null) {
  if (spaceId) {
    localStorage.setItem(ACTIVE_SPACE_KEY, spaceId);
  } else {
    localStorage.removeItem(ACTIVE_SPACE_KEY);
  }
  // Notify listeners (SWR hooks will refetch)
  window.dispatchEvent(new CustomEvent("space-changed", { detail: spaceId }));
}

/** Cache the user's space list for quick access in the switcher. */
export function cacheSpaces(spaces: CachedSpace[]) {
  localStorage.setItem(SPACE_CACHE_KEY, JSON.stringify(spaces));
}

export function getCachedSpaces(): CachedSpace[] {
  if (typeof window === "undefined") return [];
  try {
    return JSON.parse(localStorage.getItem(SPACE_CACHE_KEY) || "[]");
  } catch {
    return [];
  }
}
```

### 4.2 New file: `src/components/providers/space-provider.tsx`

A React context provider that wraps the store and triggers re-renders:

```typescript
"use client";

import { createContext, useContext, useState, useEffect, useCallback } from "react";
import { getActiveSpaceId, setActiveSpaceId as storeSetActiveSpaceId } from "@/lib/space-store";
import { spacesApi, type SpaceDoc, type SpaceLiteDoc, type SpaceMemberDoc } from "@/lib/api/spaces";

interface SpaceContextValue {
  activeSpaceId: string | null;
  activeSpace: SpaceLiteDoc | null;
  spaces: SpaceLiteDoc[];
  loading: boolean;
  switchSpace: (spaceId: string | null) => void;
  refetchSpaces: () => void;
}

const SpaceContext = createContext<SpaceContextValue | null>(null);

export function SpaceProvider({ children }: { children: React.ReactNode }) {
  const [activeSpaceId, setActiveSpaceId] = useState<string | null>(getActiveSpaceId());
  const [spaces, setSpaces] = useState<SpaceLiteDoc[]>([]);
  const [loading, setLoading] = useState(true);

  const fetchSpaces = useCallback(async () => {
    try {
      const result = await spacesApi.list({ limit: 100 });
      setSpaces(result.docs);
    } catch {
      // user may not be logged in yet
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchSpaces(); }, [fetchSpaces]);

  // Listen for cross-tab space changes
  useEffect(() => {
    const handler = (e: Event) => {
      const spaceId = (e as CustomEvent).detail;
      setActiveSpaceId(spaceId);
    };
    window.addEventListener("space-changed", handler);
    return () => window.removeEventListener("space-changed", handler);
  }, []);

  const switchSpace = useCallback((spaceId: string | null) => {
    storeSetActiveSpaceId(spaceId);
    setActiveSpaceId(spaceId);
  }, []);

  const activeSpace = spaces.find(s => s._id === activeSpaceId) ?? null;

  return (
    <SpaceContext.Provider value={{
      activeSpaceId, activeSpace, spaces, loading,
      switchSpace, refetchSpaces: fetchSpaces,
    }}>
      {children}
    </SpaceContext.Provider>
  );
}

export function useSpace() {
  const ctx = useContext(SpaceContext);
  if (!ctx) throw new Error("useSpace must be used within SpaceProvider");
  return ctx;
}
```

Add `<SpaceProvider>` inside the existing provider tree in `layout.tsx`, after the auth provider.

---

## 5. Hook Layer

### 5.1 `src/hooks/use-spaces.ts`

```typescript
"use client";

import { useApi } from "./use-api";
import { spacesApi } from "@/lib/api";

export function useSpaces() {
  return useApi(["spaces"], () => spacesApi.list({ limit: 100 }));
}

export function useSpaceMembers(spaceId: string | null) {
  return useApi(
    spaceId ? ["space-members", spaceId] : null,
    () => spacesApi.listMembers(spaceId!, { limit: 100 }),
  );
}

export function useSpaceInvitations(spaceId: string | null) {
  return useApi(
    spaceId ? ["space-invitations", spaceId] : null,
    () => spacesApi.listInvitations(spaceId!, { limit: 100 }),
  );
}

export function useMyInvitations() {
  return useApi(["my-invitations"], () => spacesApi.listMyInvitations({ limit: 100 }));
}
```

### 5.2 Update existing hooks to include space context

Existing hooks like `use-actions.ts` should include `activeSpaceId` in their SWR cache key so data refetches when the user switches spaces:

```typescript
"use client";

import { useApi } from "./use-api";
import { actionsApi } from "@/lib/api";
import { useSpace } from "@/components/providers/space-provider";

export function useActions(params?: { limit?: number; offset?: number }) {
  const { activeSpaceId } = useSpace();
  const limit = params?.limit ?? 30;
  const offset = params?.offset ?? 0;
  return useApi(
    ["actions", limit, offset, activeSpaceId],  // space_id in cache key
    () => actionsApi.list({ limit, offset }),
    // space_id is injected automatically by the API client
  );
}
```

Apply the same pattern to `use-graphs.ts`, `use-teams.ts`, and all resource hooks.

---

## 6. Shell / Navigation Changes

### 6.1 Space Switcher Component

Add a space switcher to the shell. Recommended location: `src/components/shell/space-switcher.tsx`

The switcher should appear in the top bar or icon rail, showing:
- "Personal" option (no space selected)
- List of user's spaces
- "Create Space" action

It uses the `useSpace()` hook and calls `switchSpace()` on selection.

### 6.2 Context Indicator

When a space is active, show a visual indicator in the top bar (space name, badge, or breadcrumb). This helps users know they are in a workspace context vs personal context.

### 6.3 Icon Rail

Optionally add a nav item in `icon-rail.tsx` for space settings:

```typescript
// In navItems or bottomItems:
{ icon: Building2, label: "Spaces", href: "/spaces" }
```

---

## 7. Pages and Components

### 7.1 New Pages

| Route | Purpose |
|-------|---------|
| `/spaces` | List user's spaces, create new space |
| `/spaces/[id]` | Space detail — members, settings, invitations |
| `/spaces/[id]/settings` | Space name, description, danger zone (delete) |
| `/spaces/[id]/members` | Member list, role management |
| `/spaces/[id]/invitations` | Pending invitations, send new invitation |

### 7.2 Invitation Inbox

Add an invitation indicator (badge on bell icon or inline in a dropdown). Route: `/invitations` or embedded in a popover.

Shows pending invitations with Accept / Decline actions.

### 7.3 Sharing UI on Resources

On Action, Graph, and Team detail pages, the `shared_with` field editor should support two entry types:

**Email entries** (existing behavior):
```json
"alice@example.com"
```

**Space entries** (new):
```json
{ "space_id": "sp_abc123" }
```

The UI should render these differently:
- Email → user avatar/chip with email
- SpaceId → space name badge (resolve name from space list)

When editing `shared_with`, provide an autocomplete that offers:
- User emails (type to search)
- Spaces the user is a member of (from cached space list)

---

## 8. Resource Sharing (`shared_with`)

### 8.1 Wire Format

The `shared_with` field on Actions, Graphs, and Teams is a mixed array:

```json
{
  "shared_with": [
    "alice@example.com",
    { "space_id": "sp_abc123" }
  ]
}
```

### 8.2 TypeScript Type

```typescript
interface SpaceIdEntry {
  space_id: string;
}

type SharedWithEntry = string | SpaceIdEntry;

// On resource docs:
interface ActionLiteDoc {
  // ... existing fields ...
  shared_with: SharedWithEntry[];
}
```

### 8.3 Helpers

```typescript
function isSpaceShare(entry: SharedWithEntry): entry is SpaceIdEntry {
  return typeof entry === "object" && "space_id" in entry;
}

function getEmails(sharedWith: SharedWithEntry[]): string[] {
  return sharedWith.filter((e): e is string => typeof e === "string");
}

function getSpaceIds(sharedWith: SharedWithEntry[]): string[] {
  return sharedWith
    .filter(isSpaceShare)
    .map(e => e.space_id);
}
```

---

## 9. Implementation Order

Recommended increments for the frontend, mirroring the backend increments:

### Phase 1: Foundation
1. **API client** — `spaces.ts`, export from `index.ts`
2. **Space store** — `space-store.ts`
3. **Space provider** — `space-provider.tsx`, wire into layout
4. **Hooks** — `use-spaces.ts`

### Phase 2: Space CRUD UI
5. **Spaces list page** — `/spaces` with create dialog
6. **Space detail page** — `/spaces/[id]` with settings

### Phase 3: Membership & Invitations
7. **Members page** — `/spaces/[id]/members` with role management
8. **Invitation page** — `/spaces/[id]/invitations` with send form
9. **Invitation inbox** — `/invitations` or popover with accept/decline

### Phase 4: Space Context
10. **Space switcher** — in top bar or icon rail
11. **Auto-inject `space_id`** — modify `AutobotsClient` methods
12. **Update SWR cache keys** — add `activeSpaceId` to all resource hooks
13. **Context indicator** — visual feedback for active space

### Phase 5: Sharing UI
14. **Shared-with editor** — mixed email + space entry support on resource detail pages
15. **Shared-with display** — render email chips and space badges on list views

---

## Appendix: Backend `space_id` Flow

```
Frontend (space switcher)
    │
    ▼
AutobotsClient appends ?space_id=xxx
    │
    ▼
Backend: get_optional_space_id() dependency
    ├── Reads space_id from query param
    ├── Validates membership (403 if not member)
    └── Returns space_id or None
    │
    ▼
Service layer (user_actions.py, user_action_graph.py, user_action_team.py)
    ├── find_params include space_id
    ├── or_find_queries include space_id
    └── SpaceId sharing queries when space_id is set
    │
    ▼
CRUD layer (mongo_base_crud.py)
    └── _build_find_params flattens SpaceId to dot notation
```
