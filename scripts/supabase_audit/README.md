# Supabase Auth Migration (password-preserving) — Autobots/Runkai → Bolt/Scout

## Context

We operate two separate Supabase projects:

1. **Autobots (Runkai Enterprise)** — source
   - Enterprise users exist here
   - Login method: **email + password**
   - Goal: these users must be able to log in to Bolt/Scout using the **same password** (no reset)

2. **Bolt (Scout/Bolt shared)** — destination
   - Scout users + Bolt users share the same auth project
   - Login methods: **email + password**, **Google OAuth**, **Outlook/Microsoft OAuth** (typically `azure`)

Problem:
- Supabase Auth is isolated per project. Autobots users cannot log in to Bolt/Scout unless their auth records exist in Bolt Auth.

Critical requirement:
- **Runkai enterprise users must reuse their existing password without resetting.**

This requirement materially changes the approach:
- Supabase Auth Admin API cannot export/import password hashes.
- Therefore, API-based user creation cannot preserve passwords.
- Password reuse requires **DB-level sync** of `auth.users.encrypted_password` (and related auth columns).

---

## What these scripts do

### DB-level migration scripts (writes only when explicitly enabled)
- Sync users from Autobots → Bolt while preserving password hashes
- Supports multiple modes:
  - selected/all users
  - idempotent (do not override destination users)
  - override (update destination password hash by email)

> **Important:** These sync scripts default to **dry-run**. They will not write unless `--apply --yes-apply` is provided.

---

## File structure

```
scripts/supabase_audit/
  .env.sample.supabase_audit
  .env.supabase_audit              # local only (DO NOT COMMIT)
  01_sync_auth_users.py
  02_interactive_sync.py
  common.py
  README.md
```

---

## Setup

Create a local env file (do not commit it):

```bash
cp scripts/supabase_audit/.env.sample.supabase_audit scripts/supabase_audit/.env.supabase_audit
```

The scripts will automatically look for `.env.supabase_audit` (see `common.py` search order).

### Required env vars
Add Postgres connection strings:

```bash
AUTOBOTS_DB_URL=postgresql://...
BOLT_DB_URL=postgresql://...
```

#### Note on IPv4 / pooler (important)
Prefer direct DB connections where possible. Some Supabase projects are **not IPv4 compatible** for direct DB connections (IPv6-only). In that case, use **Session Pooler** connection strings from the Supabase dashboard.

For pooler usernames, Supabase commonly uses:
- `identity_bridge.<project_ref>` (e.g. `identity_bridge.rotbrzpcxkqszmcstsju`)

If your password contains special characters (e.g. `!`) and you embed it in the URI, URL-encode it:
- `!` → `%21`

---

## Dependencies

These scripts use:
- `psycopg2-binary` (DB reads/writes)
- `python-dotenv` (optional; only if you want `.env.supabase_audit` loading)

In this repo, `psycopg2-binary` is already present in `pyproject.toml`.
If running outside the project venv, install:
```bash
pip install python-dotenv psycopg2-binary
```

---

## How to run (recommended)

From repo root:
```bash
mkdir -p tmp
python scripts/supabase_audit/02_interactive_sync.py
```

`02_interactive_sync.py` is a wrapper that calls `01_sync_auth_users.py` under the hood.

### Non-interactive (CLI)

Dry-run plan (safe mode / allowlist):
```bash
python scripts/supabase_audit/01_sync_auth_users.py --mode safe --debug
```

Apply (safe mode):
```bash
python scripts/supabase_audit/01_sync_auth_users.py \
  --mode safe \
  --apply --yes-apply \
  --copy-identities \
  --debug
```

### Troubleshooting: RLS on source auth tables (Autobots)
`auth.*` tables may have RLS enabled. A migration role may see **0 rows** unless:
- it has `BYPASSRLS` (usually requires superuser), OR
- you create explicit SELECT policies for that role.

If `01_sync_auth_users.py` reports `source_candidates=0` unexpectedly, check RLS/policies on:
- `auth.users`
- `auth.identities`

---

## Password-preserving sync (periodic)

Script:
```bash
python scripts/supabase_audit/01_sync_auth_users.py ...
```

---

## Destination (Bolt/Scout) schema notes (important in practice)

This migration writes directly to Supabase Auth tables (`auth.users`, `auth.identities`).
That is powerful, but it also means we must respect the assumptions made by the Supabase Auth
service (GoTrue) and the constraints enforced by the destination database.

### 1) `instance_id` must be set (even if `auth.instances` is empty)

In some Supabase setups, `auth.instances` can be empty even though `auth.users` is actively used.
In Bolt/Scout, GoTrue still expects users to belong to the default instance:

`00000000-0000-0000-0000-000000000000`

If we insert users with `instance_id = NULL`, they may:
- appear in SQL queries, but
- be invisible to the Supabase dashboard and Auth Admin API, and
- fail operations like “Delete user” (dashboard shows “User not found”).

`01_sync_auth_users.py` automatically chooses a destination instance id:
- If `auth.instances` has rows, it uses the first one (or `--dst-instance-id`).
- If `auth.instances` is empty, it detects the most common `auth.users.instance_id` in destination.
- If none exist, it falls back to `00000000-0000-0000-0000-000000000000`.

### 2) Some Auth columns must not be NULL (GoTrue can crash on NULL strings)

We observed GoTrue returning 500 errors like:
- “Database error loading user”
- “converting NULL to string is unsupported”

This happened when certain text columns in `auth.users` were NULL (for example:
`confirmation_token`, `email_change`, `phone_change`, etc.).

To prevent that, the sync script normalizes a small allowlist of fields:
- after insert, it converts NULL → empty string (`''`) for GoTrue-required columns.

If you ever see GoTrue 500 errors while loading a user, check the Auth logs in Supabase
for the exact column name. The sync script already covers the columns we saw in Bolt/Scout.

### 3) `phone` is UNIQUE in destination (do not normalize missing phone to empty string)

Bolt/Scout has a uniqueness constraint:

`UNIQUE(phone)`

In Postgres:
- `NULL` values do *not* collide (multiple NULL phones are allowed),
- but `''` (empty string) is a real value and *will* collide.

Therefore, the sync script treats phone specially:
- if source `phone` is empty, it inserts `NULL`,
- and it normalizes `phone` using `NULLIF(phone, '')` to avoid uniqueness collisions.

---

### Guardrails (important)
By default, the script blocks all users except a small allowlisted set defined in the script
as a module-level variable (e.g. `ALLOWED_EMAILS_DEFAULT`).
Edit that variable locally when you want to change the default safe allowlist.

To bypass guardrails you must provide:
```bash
--no-guard --yes-really
```

### Modes

#### 0) Safe mode (recommended starting point)
Safe mode is: **selected + idempotent + allowlist enforced**.

Dry-run:
```bash
python scripts/supabase_audit/01_sync_auth_users.py --mode safe --debug
```

Apply (writes):
```bash
python scripts/supabase_audit/01_sync_auth_users.py \
  --mode safe \
  --apply --yes-apply \
  --copy-identities \
  --debug
```

#### 1) Selected users, idempotent (do not override existing destination users)
Dry-run:
```bash
python scripts/supabase_audit/01_sync_auth_users.py \
  --mode selected-idempotent \
  --emails-file tmp/selected_emails.txt
```

Apply:
```bash
python scripts/supabase_audit/01_sync_auth_users.py \
  --mode selected-idempotent \
  --emails-file tmp/selected_emails.txt \
  --apply --yes-apply \
  --copy-identities
```

Note: in selected modes, if you want to operate on emails not in the default allowlist, add:
```bash
--no-guard --yes-really
```

#### 2) Selected users, override (update destination password hash if user exists)
Dry-run:
```bash
python scripts/supabase_audit/01_sync_auth_users.py \
  --mode selected-override \
  --emails-file tmp/selected_emails.txt \
  --no-guard --yes-really \
  --add-email-provider \
  --promote-confirmation \
  --skip-oauth-only-overrides \
  --debug
```

Apply:
```bash
python scripts/supabase_audit/01_sync_auth_users.py \
  --mode selected-override \
  --emails-file tmp/selected_emails.txt \
  --no-guard --yes-really \
  --apply --yes-apply \
  --add-email-provider \
  --promote-confirmation \
  --skip-oauth-only-overrides \
  --debug
```

#### 3) All users, idempotent
Dry-run:
```bash
python scripts/supabase_audit/01_sync_auth_users.py \
  --mode all-idempotent \
  --no-guard --yes-really \
  --debug
```

Apply:
```bash
python scripts/supabase_audit/01_sync_auth_users.py \
  --mode all-idempotent \
  --no-guard --yes-really \
  --apply --yes-apply \
  --copy-identities \
  --debug
```

#### 4) All users, override
Dry-run:
```bash
python scripts/supabase_audit/01_sync_auth_users.py \
  --mode all-override \
  --no-guard --yes-really \
  --add-email-provider \
  --promote-confirmation \
  --skip-oauth-only-overrides \
  --debug
```

Apply:
```bash
python scripts/supabase_audit/01_sync_auth_users.py \
  --mode all-override \
  --no-guard --yes-really \
  --apply --yes-apply \
  --add-email-provider \
  --promote-confirmation \
  --skip-oauth-only-overrides \
  --debug
```

### Dry-run vs apply
By default it is a dry-run and writes a plan report to `tmp/sync_report_*.json`.

To actually write to Bolt:
```bash
python scripts/supabase_audit/01_sync_auth_users.py --mode all-override --apply --yes-apply
```

Recommended first write test (single allowlisted user, safe mode):
```bash
python scripts/supabase_audit/01_sync_auth_users.py --mode safe --apply --yes-apply
```

Notes:
- For large runs, the script looks up destination users in chunks (`--lookup-chunk-size`).
- Use `--debug` to print DB preflight details + 1–2 sample user summaries.

---

## Recommended migration runbook (insert-only, no overrides)

This is the safest “copy missing users” rollout. It does not modify existing destination users.

### 1) Dry-run plan (ALL users)
```bash
python scripts/supabase_audit/01_sync_auth_users.py \
  --mode all-idempotent \
  --no-guard --yes-really \
  --debug
```

### 2) Apply (copy missing users + identities)
```bash
python scripts/supabase_audit/01_sync_auth_users.py \
  --mode all-idempotent \
  --no-guard --yes-really \
  --apply --yes-apply \
  --copy-identities \
  --debug
```

### 3) Re-run dry-run (should show insert=0)
```bash
python scripts/supabase_audit/01_sync_auth_users.py \
  --mode all-idempotent \
  --no-guard --yes-really \
  --debug
```

### 4) Post-migration checks (run in Bolt/Scout SQL editor)

**A. No NULL instance_id**
```sql
select count(*) as users_with_null_instance_id
from auth.users
where instance_id is null;
```

**B. No GoTrue-crash NULL fields**
```sql
select count(*) as bad_rows
from auth.users
where instance_id = '00000000-0000-0000-0000-000000000000'
  and (
    confirmation_token is null
    or recovery_token is null
    or reauthentication_token is null
    or email_change is null
    or email_change_token_current is null
    or email_change_token_new is null
    or phone_change is null
    or phone_change_token is null
  );
```

**C. Password users should have an email identity**
```sql
select count(*) as pw_users_missing_email_identity
from auth.users u
where u.encrypted_password is not null
  and not exists (
    select 1 from auth.identities i
    where i.user_id = u.id and i.provider = 'email'
  );
```

**D. No duplicate non-null phones**
```sql
select phone, count(*) as n
from auth.users
where phone is not null
group by phone
having count(*) > 1;
```

---

## What the sync script does NOT do
- It does **not** copy sessions (`auth.refresh_tokens`) from Autobots to Bolt.
- Users will sign in again in Bolt/Scout (but with the **same password** if override/insert includes the hash).

---

## Destination permissions (Bolt) for writes

For dry-run, read-only is enough.
For `--apply`, the destination role must have write permissions on Bolt:

```sql
grant insert, update on auth.users to identity_bridge;
grant insert, update on auth.identities to identity_bridge;
```

(We typically do **not** grant anything on `auth.refresh_tokens`.)

Note:
- In some Supabase/Auth setups, `auth.*` table write privileges may not be grantable to custom roles
  (you can observe this via `information_schema.role_table_grants.is_grantable`).
- In that case, run destination writes using the `postgres.<project_ref>` pooler user for `BOLT_DB_URL`
  during a controlled migration window, while keeping Autobots reads on a least-privilege role.

---

## Interactive sync (menu-driven)

Script:
```bash
python scripts/supabase_audit/02_interactive_sync.py
```

What you will be asked:
1) "Show detailed technical output?" (`y` or press Enter)
2) "How many users do you want to work with?"
   - **Test users only (SAFE)**: only a small pre-approved list
   - **ALL users (DANGEROUS)**: entire user database — requires typing `APPLY` to continue

What you can do in the menu:
- Refresh the plan (re-check current status)
- View users missing in destination (can be copied)
- View users already in destination (overlap/conflicts)
- Apply actions:
  - Copy selected missing users to destination
  - Update passwords for selected existing users  **DANGEROUS**
  - Copy ALL missing users to destination
  - Update passwords for ALL existing users **VERY DANGEROUS**

Selecting users:
- When a list is shown, you can choose user numbers using formats like:
  - `1,2,5-9`
- Before any write, the tool will require you to type `APPLY` to confirm.

Note:
- `02_interactive_sync.py` is a wrapper that calls `01_sync_auth_users.py` under the hood.
- All writes still require `--apply --yes-apply` (the interactive tool triggers these only after confirmation).

Tip:
- If you want insert-only migration using the menu, choose **ALL users** scope and then select:
  **“Copy ALL missing users to destination”**.

---

## Expected migration observations (current reality)

We observed:
- Autobots: provider mostly/all `email`
- Bolt: mix of `email`, `google`, `azure`
- Overlap exists, and overlapping emails have **different UUIDs**

Therefore a safe password-preserving policy is:
- If email is missing in Bolt: **INSERT** user with Autobots `encrypted_password`
- If email exists in Bolt:
  - idempotent mode: **SKIP**
  - override mode: **UPDATE** Bolt `encrypted_password` by email (do not change Bolt UUID)

---

## Security

- DB URLs/passwords are highly privileged.
- Keep `.env.supabase_audit` **local only** and **gitignored**.
- Prefer least-privilege DB roles (read-only for dry-run/planning; scoped write only for sync).
