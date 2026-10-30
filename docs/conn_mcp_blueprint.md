# MCP connection forwarding — implementation blueprint (AUT-1593, deferred)

> **Reader's note (added when this document was ported into runkailabs/autobots).**
> This blueprint was written in the *origin* repo (jetoslabs/autobots), where AUT-1593
> was first built, and it has been kept as written rather than rewritten. Read its
> point of view accordingly:
>
> - "this repo" throughout means **jetoslabs/autobots**, where the MCP bridge was built
>   and then removed before shipping;
> - "the sibling repo `runkailabs/autobots`", "the reuse target", and "runkailabs'
>   existing client" all mean **this repo, the one you are reading the file in**.
>
> So the two defects described in section 2 are defects in *this* codebase's
> `src/autobots/mcp/mcp_client.py`, and section 2 is the part to act on first. The rest
> of AUT-1593 (app-connection forwarding, the database bridge, direct database
> materialization) has now landed here; MCP forwarding is still the deferred piece this
> document describes.

## 1. Status and context

**MCP connection forwarding is not implemented in this repo.** A Daytona sandbox run can
forward app credentials (`forward_connections`), read-only database access through the
query bridge (`forward_databases`), and direct database connection strings
(`forward_databases_direct`). It cannot reach the user's saved MCP servers, and there is
no `forward_mcp_servers` config field.

This was a deliberate deferral, not an oversight. A working MCP bridge — config field,
grant model, two bridge endpoints, and a server-side MCP client — was built and reviewed
in this repo, then removed before shipping.

The reason: this feature is being ported to the sibling repo `runkailabs/autobots`, which
**already has a live MCP client** at `src/autobots/mcp/mcp_client.py` (a real path
collision with the client that was deleted from this repo). Their client exposes
`MCPClientWrapper` with `connect`, `list_tools`, `call_tool`, `call_tool_from_binding`, and
`filter_allowed_tools`, and it is in production today backing OpenRouter's MCP
tool-calling path. Shipping a second, parallel MCP client here would be throwaway work —
it would be discarded during the port in favour of theirs.

So the useful artifact is not the code; it is the design, plus a precise account of the
two things their existing client gets wrong that the bridge must not inherit. That is
what this document is.

The pre-existing MCP infrastructure in this repo — `src/autobots/mcp/mcp_doc_model.py`,
`mcp_crud.py`, `user_mcp.py` (storage and CRUD for a user's saved MCP connections, used
elsewhere in the platform) — is untouched and still available to build on.

## 2. The two things the next implementation MUST get right

These are the reason this document exists. The reuse target's existing client gets both
wrong today, and both fail *silently* and *permissively* — nothing errors, access is
simply wider than intended.

### 2.1 Fail-closed tool allowlisting

**The problem.** runkailabs' `MCPClientWrapper.filter_allowed_tools` **fails open**: an
empty or unset allowlist is treated as "no restriction", so every tool the MCP server
advertises is permitted. That default is defensible for their OpenRouter path, where the
user is interactively driving their own connection. It is exactly backwards for a sandbox
bridge, where the caller is arbitrary code running on the user's behalf and the whole
point of the grant list is to bound what that code can reach.

**Consequence if inherited:** a `forward_mcp_servers` grant that names a server but sets
no `allowed_tools`, against a saved connection that also sets none, would grant the
sandbox *every tool on that server*. Two omissions silently compose into full access.

**The rule.** The bridge layer must **not** delegate authorization to
`filter_allowed_tools`. It must compute its own effective-tools intersection, and:

- A side that **has not declared** a restriction is "unrestricted on that side" — it
  contributes no constraint, but it also grants nothing on its own.
- A side that **has declared** a list, *including an explicitly empty one*, is exactly
  that set. `[]` means "this side grants no tools", not "this side has no opinion".
- If **neither** side declared a restriction, **reject the request**. The absence of a
  restriction anywhere is not permission; it is an unconfigured grant.
- Otherwise, intersect.

Use a sentinel object rather than `None`, so "unset" is representable as a value distinct
from any set (including the empty set). The exact algorithm validated here:

```python
_ALL_TOOLS = object()  # sentinel: "this side declares no restriction of its own"


def _effective_tools(
    job_side_tools: Optional[List[str]], mcp_doc_allowed_tools: Optional[List[str]]
) -> set[str]:
    """
    Intersect the job's grant with the saved connection's allowlist.

    Fails closed: if neither side declares an allowlist, no tool is permitted. A
    missing allowlist is not permission to call everything.
    """
    # `is not None` matters here: an explicit empty list means "this side grants no
    # tools" and must stay an empty set, not fall through to the "unrestricted" sentinel
    # the way a falsy-but-unset value would with a plain truthiness check.
    job_side: Any = set(job_side_tools) if job_side_tools is not None else _ALL_TOOLS
    doc_side: Any = (
        set(mcp_doc_allowed_tools) if mcp_doc_allowed_tools is not None else _ALL_TOOLS
    )
    if job_side is _ALL_TOOLS and doc_side is _ALL_TOOLS:
        raise HTTPException(
            status_code=403,
            detail="No allowed_tools configured on the request or the saved connection",
        )
    if job_side is _ALL_TOOLS:
        return doc_side
    if doc_side is _ALL_TOOLS:
        return job_side
    return job_side & doc_side
```

Both endpoints consume the result: `tools/list` filters its response through it,
`tools/call` rejects with 403 when the requested tool is not in it. `tools/list` must
filter rather than merely advise — an unfiltered listing tells the sandbox about tools it
cannot call and invites the caller to probe.

**Flagging the specific bug by name, because it is easy to reintroduce:**

```python
job_side = set(x) if x else _ALL_TOOLS          # WRONG — [] falls through to unrestricted
job_side = set(x) if x is not None else _ALL_TOOLS   # RIGHT
```

The truthiness form was written in this repo's own first draft of this logic and was
caught in review. It reads as correct, passes any test that only exercises unset-vs-
populated, and turns an explicit "grant nothing" into "grant everything" — the single
worst possible inversion — only when someone actually writes `allowed_tools: []`. Use
`is not None`, and cover the explicit-empty-list case in tests.

### 2.2 SSRF protection before connecting

**The problem.** runkailabs' `MCPClientWrapper.connect` performs **zero URL or IP
validation** before connecting to a saved `server_url`. MCP server URLs are user-supplied
and are fetched *by the API server*, from inside the production network. A user who
registers an MCP connection pointing at `http://169.254.169.254/` (cloud instance
metadata) or at any internal service will have the API server connect to it and return the
response. That is a straightforward SSRF, and it exists on their live OpenRouter path
today — it is not created by the bridge, only exposed further by it.

**The rule.** Add a pre-flight guard that runs before *any* connection attempt:

1. Require the scheme to be exactly `https`. Reject `http`, `file`, and everything else.
2. Require a hostname.
3. Resolve it with `socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)`.
4. Parse **every** returned address with `ipaddress.ip_address` (strip any IPv6 zone
   index — split on `%`) and reject if *any* of them is loopback, link-local, private
   (RFC1918), multicast, reserved, or unspecified — i.e. `.is_loopback`, `.is_link_local`,
   `.is_private`, `.is_multicast`, `.is_reserved`, `.is_unspecified` — or is a cloud
   metadata address (`169.254.169.254`, `fd00:ec2::254`; covered by the link-local check,
   but worth naming explicitly so the intent survives future edits).

Reject on *any* resolved address, not just the first: a hostname resolving to one public
and one private address must be refused.

Also disable redirect-following on the underlying HTTP client. A validated public host
that 302s to an internal one defeats the guard entirely, and the MCP SDK's default client
follows redirects.

Because `socket.getaddrinfo` blocks, call the guard via `asyncio.to_thread` from async
code.

**What this does not close — state it plainly.** This is a pre-flight check, so it does
**not** close the DNS-rebinding variant: the hostname is resolved once for validation and
then resolved *again*, independently, by the HTTP client at actual connection time. An
attacker controlling the DNS response can return a public address to the first lookup and
an internal one to the second. Our own implementation did not close this either. Closing
it properly means pinning the validated address for the real connection — connecting to
the vetted IP with the original hostname carried in the `Host` header and SNI — which
requires lower-level control over the HTTP client than the `mcp` SDK's high-level
transport helpers (`sse_client`, `streamable_http_client`) expose. If the threat model
warrants it, that is the fix; do not pretend a second independent resolution is safe.

**Where it should live.** Contribute this as a small standalone module in the runkailabs
repo — e.g. `src/autobots/mcp/mcp_url_guard.py` — that `MCPClientWrapper.connect` calls
into, rather than bolting it onto the bridge endpoints. Placing it in `connect` makes it a
security improvement to their existing production OpenRouter MCP path as well, and means
no future caller of the client can forget it. A guard that only the bridge calls protects
only the bridge.

## 3. The bridge contract to reimplement

Mirror the already-working read-only DB bridge in this repo
(`src/autobots/api/v1/api_daytona/api_daytona.py`, `POST
/daytona/connections/databases/{connection_name}/query`) rather than inventing a parallel
shape. Concretely:

### Config field

On `DaytonaSandboxConfig` in
`src/autobots/action/action_type/action_text2text/action_text2text_daytona.py`, alongside
`forward_databases` / `forward_databases_direct`:

```python
class McpForwardGrant(BaseModel):
    """One MCP server a sandbox run may reach through the connections bridge."""

    server_name: str
    allowed_tools: List[str] | None = None


forward_mcp_servers: List[McpForwardGrant] = Field(
    default_factory=list,
    description=(
        "MCP servers usable via the connections bridge during this run, each with its "
        "own tool allowlist."
    ),
)
```

`allowed_tools` is `List[str] | None`, **not** `List[str] = []` — the difference between
unset and explicitly-empty is load-bearing per §2.1 and must survive into the persisted
grant.

### Endpoints

Two, under the existing `/daytona/connections` prefix:

- `POST /connections/mcps/{server_name}/tools/list` → `{"tools": [...]}`
- `POST /connections/mcps/{server_name}/tools/call`, body `{"tool": str, "arguments": dict}`
  → `{"result": ...}`

Both are intentionally unauthenticated in the JWT sense, exactly like the DB bridge and
`/presign-put`: a sandbox cannot hold the user's JWT.

### Authorization — reuse, do not invent

Use the identical mechanism the DB bridge already uses. Do not add a new auth path:

1. `verify_connections_token(_extract_bearer(authorization))` — the signed, job-scoped
   `AUTOBOTS_CONNECTIONS_TOKEN` bearer token (HMAC over `JWT_SECRET_KEY`), minted in
   `run_tool` and injected into the sandbox's *process* environment only.
2. `_load_active_job(db=db, payload=payload)` — re-checks on every call that the job still
   exists, is `queued`/`running`, and has not had `connections_revoked` flipped.
3. `_resolve_bridge_user(...)` with `job["credential_space_id"]` — reattaches space
   membership (re-resolved fresh, failing closed if revoked) so space-scoped MCP
   connections resolve under the scope they were saved with.
4. Per-call grant check against the job document, mirroring the DB endpoint's
   `if connection_name not in (job.get("forward_databases") or []):` → 403.

### Job document

Persist the grants at job-creation time, the same way `forward_databases` already is. In
`src/autobots/conn/daytona/state/daytona_job_model.py`:

```python
# Stored as plain dicts ({"server_name": str, "allowed_tools": list[str] | None})
# to keep this connector model independent of the action layer's config models.
forward_mcp_servers: List[Dict[str, Any]] = Field(default_factory=list)
```

written in `run_tool` as
`forward_mcp_servers=[g.model_dump() for g in (action_config.forward_mcp_servers or [])]`.
Defaulted so previously persisted job documents stay valid.

Grants are read from the *job document*, never from the request. The sandbox names a
server; the server decides whether that job was granted it.

### Wiring in `run_tool`

Three existing spots take `or action_config.forward_mcp_servers`, all of which were
present and are now removed:

- the callback-base guard — bridged connections need `DAYTONA_CALLBACK_BASE_URL` /
  `APP_BASE_URL`; materialized ones do not;
- `has_forwarded`, which drives `sandbox_owner_user_id` (pinning a
  connection-enabled sandbox to one credential subject) and terminal-state revocation;
- the block that mints `AUTOBOTS_CONNECTIONS_URL` / `AUTOBOTS_CONNECTIONS_TOKEN`, which
  today reads `if action_config.forward_databases:` and must become
  `if action_config.forward_databases or action_config.forward_mcp_servers:`.

Revocation needs no new work: `/connections/revoke`, the terminal-status flip, and the
cancel path all key off `connections_revoked` and `_job_has_forwarded_connections`, and
cover MCP for free once the field is back in both.

### LLM-facing description

`get_description()` currently states plainly that MCP forwarding is unavailable. Replace
that line when implementing — and when doing so, document the empty-list semantics
explicitly, because the model is the one filling in `allowed_tools`: an `allowed_tools` of
`[]` grants nothing, and omitting it on *both* sides is a rejected call, not an
unrestricted one.

## 4. What NOT to bring over from this repo

**The deleted `src/autobots/mcp/mcp_client.py`.** It is gone deliberately. Build the
bridge against runkailabs' `MCPClientWrapper` instead. Resurrecting a parallel client at
the same path in the same package would collide with a module that is live in production
there, and would leave two clients to keep in sync — with the security fixes in §2 applied
to only one of them.

The one piece of that file worth carrying across is its SSRF guard, and it should land as
a new module inside `MCPClientWrapper.connect`'s call path (§2.2), not as a client.

Also do not bring over the `mcp==2.1.1` dependency — it was added here solely for the
deleted client and has been removed. The reuse target already depends on whatever SDK its
client uses.

## 5. Why this document exists instead of code

The MCP bridge was designed in detail before it was deferred. That work is recorded across
this repo's review rounds — `docs/conn_codex_v1.md` … `v5.md` and `docs/conn_claude_v1.md`
… `v5.md` — which cover the bridge endpoints, the grant/allowlist intersection, the
job-document persistence, and the sandbox-side helpers, along with the review that caught
the truthiness bug in §2.1.

Those documents are a design conversation: long, iterative, and full of options that were
considered and dropped. This document is the distilled, actionable remainder — the
decisions that survived, plus the two security properties the intended reuse target does
not currently satisfy. It is written so the next implementation can be built directly
against runkailabs' existing client without re-deriving any of the above.
