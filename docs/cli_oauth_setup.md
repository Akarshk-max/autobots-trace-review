# CLI OAuth (social login) — setup & flow

How a **headless CLI** (a public OAuth client that does not live in this repo) signs a
user in through Autobots using Supabase/GoTrue PKCE. Covers the end-to-end flow, the
backend endpoints involved, and the **Supabase redirect-URL configuration** it depends on.

The design is **stateless**: the backend stores nothing between the two calls. The PKCE
`verifier` is held by the CLI and never travels through the browser.

## Endpoints

All under the `/v1/auth` prefix (`API_v1` `/v1` + api_auth router prefix `/auth`):

| Endpoint | Purpose |
|----------|---------|
| `GET  /v1/auth/oauth/url?provider=&redirect_to=` | Start login. Returns `{url, verifier}`. |
| `GET  /v1/auth/oauth/cli-callback` | Browser landing page (paste-back variant). Renders one opaque token wrapping the `code` to copy. |
| `POST /v1/auth/oauth/exchange` | Exchange `{code, verifier}` for a session (`AuthResponse`). |
| `POST /v1/auth/session/refresh?refresh_token=` | Refresh the session later. |

`provider` is one of `google`, `azure` (Azure = Outlook / Microsoft).

## Flow

Two variants share the same two API calls; only `redirect_to` differs.

### Paste-back (SSH / no loopback server)

1. **CLI → `GET /v1/auth/oauth/url`** with `redirect_to = <api>/v1/auth/oauth/cli-callback`.
   Backend returns `{url, verifier}`. The CLI **stores `verifier` locally** and opens `url`
   in the user's browser.
2. **Browser**: user authenticates at Supabase → provider → GoTrue redirects the browser to
   `redirect_to` with `?code=…` (GoTrue also appends its own reserved `state`, which we ignore).
3. **Browser lands on `/v1/auth/oauth/cli-callback`**, which renders a single opaque token =
   `base64url({code})`. The user copies it. (The `verifier` is **not** in this token.)
4. **CLI**: user pastes the token → CLI base64url-decodes → `{code}`, then calls
   **`POST /v1/auth/oauth/exchange`** `{code, verifier}` (its stored verifier).
5. Backend exchanges `code`+`verifier` at GoTrue (`/token?grant_type=pkce`) and returns the
   session (access + refresh token). CLI persists it; refreshes later via `/session/refresh`.

### Loopback (desktop with a browser)

Identical, except step 1 passes `redirect_to = http://localhost:<port>/callback`. The CLI
runs a tiny local server that catches `?code&state` directly, so steps 3–4 (the paste page)
are skipped.

### Why the verifier never enters the browser

`code` alone is useless — GoTrue only mints a session for `code` **+** the matching PKCE
`verifier`, which only ever exists on the CLI. So the paste token / browser round-trip carries
nothing that can be replayed without the CLI's local secret.

## Supabase redirect-URL configuration (required)

GoTrue only redirects to `redirect_to` values on its allow-list. Add these in
**Dashboard → Authentication → URL Configuration → Redirect URLs** (the allow-list, *not*
Site URL), in the project whose ref matches that environment's `SUPABASE_URL`.

> Local and dev use **different Supabase projects** — add the entries to each one separately.

**Paste-back** — the backend landing page, one per API host:

```
http://localhost:8000/v1/auth/oauth/cli-callback
http://127.0.0.1:8000/v1/auth/oauth/cli-callback
https://dev.api.myautobots.com/v1/auth/oauth/cli-callback
https://<prod-api-host>/v1/auth/oauth/cli-callback
```

**Loopback** — CLI uses a random local port, so use Supabase glob wildcards
(`*` = one segment, no `/`; `**` = anything incl. `/`):

```
http://localhost:*/**
http://127.0.0.1:*/**
```

Prefer an exact URL over the wildcard if the CLI uses a fixed loopback port/path.

### Gotchas

- **Exact match** after wildcards: scheme, host, port, and full path all matter. The path is
  `/v1/auth/oauth/cli-callback` (note the `/v1` prefix) — `http` vs `https` and a trailing
  slash both count.
- **`redirect_to` the CLI sends must equal an allow-list entry** — it's passed straight through
  to GoTrue by `GET /v1/auth/oauth/url`.
- Wildcards are glob, not regex — `.` is literal. Don't over-broaden (e.g. never `https://**`).
- Behind a path-stripping proxy, confirm the browser actually reaches
  `/v1/auth/oauth/cli-callback` on that host before whitelisting it.

### Why we do NOT send our own `state`

`state` is **reserved and managed by GoTrue** — it round-trips a signed JWT that encodes the
flow (provider, `code_challenge`, and your `redirect_to`). If you pass your own `state` to
`/authorize`, it overwrites that JWT, so GoTrue rejects its own callback with
`error_code=bad_oauth_state` and, having lost the `redirect_to` from the unparseable state,
falls back to the Supabase **Site URL** (the later exchange then fails with
`flow_state_not_found`).

CSRF/binding is instead provided by **PKCE**: the authorization code is useless without the
`verifier`, which only the CLI holds and which never enters the browser. This is exactly the
supabase-js model. Don't reintroduce a custom `state` param.
