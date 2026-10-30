#!/usr/bin/env python3
"""
Password-preserving Auth user sync: Autobots (source) -> Bolt/Scout (destination)
===============================================================================

Modes
-----
  - safe                 (default) allowlist-only, selected, idempotent
  - selected-idempotent  selected emails, insert-only
  - selected-override    selected emails, insert + override existing password
  - all-idempotent       all source users, insert-only
  - all-override         all source users, insert + override existing password

Guardrail (current phase)
-------------------------
By default, only a small allowlisted set is processed. The default allowlist lives in the
module-level variable:
  ALLOWED_EMAILS_DEFAULT
Edit that set locally to change the default safe allowlist.

To bypass (dangerous):
  --no-guard --yes-really

Apply safety
------------
Dry-run by default.
Writes happen only with:
  --apply --yes-apply

Quick examples
--------------
Safe mode (dry-run):
  python scripts/supabase_audit/01_sync_auth_users.py --mode safe --debug

Safe mode (apply + copy identities):
  python scripts/supabase_audit/01_sync_auth_users.py --mode safe --apply --yes-apply --copy-identities --debug

Selected idempotent (apply):
  python scripts/supabase_audit/01_sync_auth_users.py --mode selected-idempotent --emails-file tmp/selected.txt --apply --yes-apply --copy-identities

Selected override (apply; dangerous):
  python scripts/supabase_audit/01_sync_auth_users.py --mode selected-override --emails-file tmp/conflicts.txt --no-guard --yes-really --apply --yes-apply --add-email-provider --promote-confirmation --skip-oauth-only-overrides

All idempotent (apply; guardrails off):
  python scripts/supabase_audit/01_sync_auth_users.py --mode all-idempotent --no-guard --yes-really --apply --yes-apply --copy-identities

All override (apply; very dangerous):
  python scripts/supabase_audit/01_sync_auth_users.py --mode all-override --no-guard --yes-really --apply --yes-apply --add-email-provider --promote-confirmation --skip-oauth-only-overrides

DB requirements
---------------
AUTOBOTS_DB_URL, BOLT_DB_URL in scripts/supabase_audit/.env.supabase_audit

Important: We write directly to auth.users / auth.identities. This is inherently sensitive.
Use dry-run first, then apply for a single allowlisted user, then expand slowly.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Set

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.supabase_audit.common import (
    env_optional,  # noqa: E402
    load_dotenv_if_available,  # noqa: E402
)

LOG = logging.getLogger("supabase_sync")

ALLOWED_EMAILS_DEFAULT = {"sourav@runkai.ai"}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm_email(e: str) -> str:
    return (e or "").strip().lower()


def chunks(seq: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _connect(db_url: str, *, readonly: bool):
    import psycopg2  # type: ignore
    from psycopg2.extras import register_default_json, register_default_jsonb, register_uuid

    conn = psycopg2.connect(db_url)
    register_default_json(conn)
    register_default_jsonb(conn)
    register_uuid(conn_or_curs=conn)

    conn.set_session(readonly=readonly, autocommit=readonly)

    cur = conn.cursor()
    cur.execute("set statement_timeout = '60s';")
    cur.execute("set lock_timeout = '5s';")
    cur.close()
    return conn


def _fetchall(conn, sql: str, params: Optional[Sequence[Any]] = None) -> list[tuple[Any, ...]]:
    cur = conn.cursor()
    cur.execute(sql, params or ())
    rows = cur.fetchall()
    cur.close()
    return rows


def _fetchone(conn, sql: str, params: Optional[Sequence[Any]] = None) -> Optional[tuple[Any, ...]]:
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    return row


def _json_param(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        from psycopg2.extras import Json  # type: ignore

        return Json(v)
    return v


@dataclass
class DbInfo:
    label: str
    current_user: str
    session_user: str
    dbname: str
    client_addr: Any
    server_addr: Any
    version: str
    rolbypassrls: bool
    rls_users: bool
    users_count: int
    identities_count: int
    refresh_tokens_count: int
    has_insert_users: bool
    has_update_users: bool
    has_insert_identities: bool
    instances_count: int
    dst_instance_id: Optional[str]
    users_instance_id_nullable: bool
    users_confirmed_at_generated: bool
    dst_instance_id_source: str
    users_null_to_empty_cols: list[str]
    users_empty_to_null_cols: list[str]


DEFAULT_INSTANCE_ID = "00000000-0000-0000-0000-000000000000"

GOTRUE_NULL_TO_EMPTY_COLS_DEFAULT = [
    "confirmation_token",
    "recovery_token",
    "reauthentication_token",
    "email_change",
    "email_change_token_current",
    "email_change_token_new",
    "phone_change",
    "phone_change_token",
]

GOTRUE_EMPTY_TO_NULL_COLS_DEFAULT = ["phone"]


def _colset(conn, schema: str, table: str) -> Set[str]:
    rows = _fetchall(
        conn,
        """
      select column_name
      from information_schema.columns
      where table_schema=%s and table_name=%s
    """,
        (schema, table),
    )
    return {str(r[0]) for r in rows}


def preflight(
    conn, *, label: str, want_write: bool, dst_instance_id_override: Optional[str]
) -> DbInfo:
    who = _fetchone(
        conn,
        "select current_user, session_user, current_database(), inet_client_addr(), inet_server_addr();",
    )
    assert who is not None
    current_user, session_user, dbname, client_addr, server_addr = who

    ver = _fetchone(conn, "select version();")
    assert ver is not None

    role = _fetchone(
        conn,
        """
        select rolbypassrls
        from pg_roles
        where rolname = current_user;
        """,
    )
    assert role is not None
    rolbypassrls = bool(role[0])

    rls_users = _fetchone(
        conn,
        """
        select c.relrowsecurity
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname='auth' and c.relname='users';
        """,
    )
    assert rls_users is not None

    users_count = _fetchone(conn, "select count(*) from auth.users;")
    identities_count = _fetchone(conn, "select count(*) from auth.identities;")
    refresh_count = _fetchone(conn, "select count(*) from auth.refresh_tokens;")
    assert users_count and identities_count and refresh_count

    has_insert_users = bool(
        _fetchone(conn, "select has_table_privilege(current_user, 'auth.users', 'INSERT');")[0]
    )
    has_update_users = bool(
        _fetchone(conn, "select has_table_privilege(current_user, 'auth.users', 'UPDATE');")[0]
    )
    has_insert_identities = bool(
        _fetchone(conn, "select has_table_privilege(current_user, 'auth.identities', 'INSERT');")[0]
    )

    inst_cnt_row = _fetchone(conn, "select count(*) from auth.instances;")
    instances_count = int(inst_cnt_row[0]) if inst_cnt_row else 0

    dst_instance_id: Optional[str] = None
    dst_instance_id_source = "none"
    if dst_instance_id_override:
        dst_instance_id = dst_instance_id_override
        dst_instance_id_source = "cli(--dst-instance-id)"
    else:
        if instances_count > 0:
            inst = _fetchone(conn, "select id from auth.instances order by id limit 1;")
            dst_instance_id = str(inst[0]) if inst else None
            dst_instance_id_source = "auth.instances(first)"
        else:
            row = _fetchone(
                conn,
                """
                select instance_id
                from auth.users
                where instance_id is not null
                group by instance_id
                order by count(*) desc
                limit 1;
                """,
            )
            if row and row[0]:
                dst_instance_id = str(row[0])
                dst_instance_id_source = "auth.users(mode)"
            else:
                dst_instance_id = DEFAULT_INSTANCE_ID
                dst_instance_id_source = "default(00000000-0000-0000-0000-000000000000)"

    nullable_row = _fetchone(
        conn,
        """
        select is_nullable
        from information_schema.columns
        where table_schema='auth' and table_name='users' and column_name='instance_id';
        """,
    )
    users_instance_id_nullable = (nullable_row and nullable_row[0] == "YES") or False

    confirmed_gen_row = _fetchone(
        conn,
        """
        select is_generated
        from information_schema.columns
        where table_schema='auth' and table_name='users' and column_name='confirmed_at';
        """,
    )
    users_confirmed_at_generated = bool(confirmed_gen_row and confirmed_gen_row[0] == "ALWAYS")

    users_cols = _colset(conn, "auth", "users")
    users_null_to_empty_cols = sorted(
        [c for c in GOTRUE_NULL_TO_EMPTY_COLS_DEFAULT if c in users_cols]
    )
    users_empty_to_null_cols = sorted(
        [c for c in GOTRUE_EMPTY_TO_NULL_COLS_DEFAULT if c in users_cols]
    )

    info = DbInfo(
        label=label,
        current_user=str(current_user),
        session_user=str(session_user),
        dbname=str(dbname),
        client_addr=client_addr,
        server_addr=server_addr,
        version=str(ver[0]),
        rolbypassrls=rolbypassrls,
        rls_users=bool(rls_users[0]),
        users_count=int(users_count[0]),
        identities_count=int(identities_count[0]),
        refresh_tokens_count=int(refresh_count[0]),
        has_insert_users=has_insert_users,
        has_update_users=has_update_users,
        has_insert_identities=has_insert_identities,
        instances_count=instances_count,
        dst_instance_id=dst_instance_id,
        users_instance_id_nullable=users_instance_id_nullable,
        users_confirmed_at_generated=users_confirmed_at_generated,
        dst_instance_id_source=dst_instance_id_source,
        users_null_to_empty_cols=users_null_to_empty_cols,
        users_empty_to_null_cols=users_empty_to_null_cols,
    )

    LOG.info(
        "[%s] user=%s session=%s db=%s client=%s server=%s bypassrls=%s rls(auth.users)=%s users=%s identities=%s refresh_tokens=%s",
        label,
        info.current_user,
        info.session_user,
        info.dbname,
        info.client_addr,
        info.server_addr,
        info.rolbypassrls,
        info.rls_users,
        info.users_count,
        info.identities_count,
        info.refresh_tokens_count,
    )
    if want_write:
        LOG.info(
            "[%s] write privs: auth.users INSERT=%s UPDATE=%s | auth.identities INSERT=%s",
            label,
            info.has_insert_users,
            info.has_update_users,
            info.has_insert_identities,
        )

    if label == "dst":
        if info.instances_count == 0:
            LOG.info("[dst] auth.instances empty (count=0).")
        else:
            LOG.info(
                "[dst] auth.instances count=%s, chosen dst_instance_id=%s",
                info.instances_count,
                info.dst_instance_id,
            )

        if info.instances_count == 0 and not info.users_instance_id_nullable:
            raise SystemExit(
                "Destination auth.instances is empty but auth.users.instance_id is NOT NULL. "
                "Cannot safely insert users."
            )

        if info.dst_instance_id is None:
            raise SystemExit(
                "Destination dst_instance_id could not be determined. Provide --dst-instance-id explicitly."
            )

        LOG.info(
            "[dst] using dst_instance_id=%s (source=%s)",
            info.dst_instance_id,
            info.dst_instance_id_source,
        )
        if info.users_null_to_empty_cols:
            LOG.info(
                "[dst] will coalesce NULL->'' for auth.users columns: %s",
                info.users_null_to_empty_cols,
            )
        if info.users_empty_to_null_cols:
            LOG.info(
                "[dst] will normalize ''->NULL for auth.users columns: %s",
                info.users_empty_to_null_cols,
            )

        if info.instances_count > 1 and not dst_instance_id_override:
            raise SystemExit(
                "Destination has multiple auth.instances rows. Provide --dst-instance-id explicitly."
            )
        if info.users_confirmed_at_generated:
            LOG.info(
                "[dst] auth.users.confirmed_at is GENERATED ALWAYS; will not insert/update confirmed_at."
            )

    LOG.debug("[%s] version=%s", label, info.version)
    return info


def load_selected_emails(args: argparse.Namespace) -> list[str]:
    emails: list[str] = []
    if args.emails:
        for chunk_str in args.emails:
            emails.extend([x.strip() for x in chunk_str.split(",") if x.strip()])
    if args.emails_file:
        p = Path(args.emails_file)
        text = p.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            emails.append(line)
    out = []
    seen = set()
    for e in emails:
        ne = norm_email(e)
        if ne and ne not in seen:
            seen.add(ne)
            out.append(ne)
    return out


def enforce_allowlist(emails: list[str], allowlist: set[str], *, no_guard: bool) -> list[str]:
    if no_guard:
        return emails
    filtered = [e for e in emails if e in allowlist]
    blocked = [e for e in emails if e not in allowlist]
    if blocked:
        LOG.warning("Guardrail active: blocked %d emails (example: %s)", len(blocked), blocked[:3])
    return filtered


def summarize_user_row(u: dict[str, Any]) -> dict[str, Any]:
    raw_app = u.get("raw_app_meta_data") or {}
    providers = raw_app.get("providers") if isinstance(raw_app, dict) else None
    provider = raw_app.get("provider") if isinstance(raw_app, dict) else None
    return {
        "id": str(u.get("id")),
        "email": u.get("email"),
        "is_sso_user": u.get("is_sso_user"),
        "is_anonymous": u.get("is_anonymous"),
        "provider": provider,
        "providers": providers,
        "has_password_hash": bool(u.get("encrypted_password")),
        "email_confirmed_at": str(u.get("email_confirmed_at"))
        if u.get("email_confirmed_at")
        else None,
        "last_sign_in_at": str(u.get("last_sign_in_at")) if u.get("last_sign_in_at") else None,
    }


def fetch_src_users(src_conn, *, all_mode: bool, emails: list[str]) -> dict[str, dict[str, Any]]:
    if all_mode:
        rows = _fetchall(
            src_conn,
            """
            select
              id, email, aud, role, encrypted_password,
              email_confirmed_at, confirmed_at,
              created_at, updated_at, last_sign_in_at,
              raw_app_meta_data, raw_user_meta_data,
              phone, phone_confirmed_at,
              is_sso_user, is_anonymous,
              banned_until, deleted_at
            from auth.users
            where email is not null
            """,
        )
    else:
        rows = _fetchall(
            src_conn,
            """
            select
              id, email, aud, role, encrypted_password,
              email_confirmed_at, confirmed_at,
              created_at, updated_at, last_sign_in_at,
              raw_app_meta_data, raw_user_meta_data,
              phone, phone_confirmed_at,
              is_sso_user, is_anonymous,
              banned_until, deleted_at
            from auth.users
            where lower(email) = any(%s)
            """,
            (emails,),
        )

    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        (
            uid,
            email,
            aud,
            role,
            encrypted_password,
            email_confirmed_at,
            confirmed_at,
            created_at,
            updated_at,
            last_sign_in_at,
            raw_app_meta_data,
            raw_user_meta_data,
            phone,
            phone_confirmed_at,
            is_sso_user,
            is_anonymous,
            banned_until,
            deleted_at,
        ) = r
        e = norm_email(email)
        if not e:
            continue
        out[e] = {
            "id": uid,
            "email": email,
            "aud": aud,
            "role": role,
            "encrypted_password": encrypted_password,
            "email_confirmed_at": email_confirmed_at,
            "confirmed_at": confirmed_at,
            "created_at": created_at,
            "updated_at": updated_at,
            "last_sign_in_at": last_sign_in_at,
            "raw_app_meta_data": raw_app_meta_data,
            "raw_user_meta_data": raw_user_meta_data,
            "phone": phone,
            "phone_confirmed_at": phone_confirmed_at,
            "is_sso_user": is_sso_user,
            "is_anonymous": is_anonymous,
            "banned_until": banned_until,
            "deleted_at": deleted_at,
        }
    return out


def fetch_dst_users_chunked(
    dst_conn, *, emails: list[str], chunk_size: int
) -> dict[str, dict[str, Any]]:
    """
    Destination lookup by email in chunks to avoid huge ANY(array) payloads in all-mode.
    Also detects duplicate destination rows for the same normalized email and aborts.
    """
    out: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}

    for batch in chunks(emails, max(1, chunk_size)):
        rows = _fetchall(
            dst_conn,
            """
            select
              id, email, aud, role, encrypted_password,
              email_confirmed_at, confirmed_at,
              created_at, updated_at, last_sign_in_at,
              raw_app_meta_data, raw_user_meta_data,
              phone, phone_confirmed_at,
              is_sso_user, is_anonymous,
              banned_until, deleted_at
            from auth.users
            where lower(email) = any(%s)
            """,
            (batch,),
        )

        for r in rows:
            (
                uid,
                email,
                aud,
                role,
                encrypted_password,
                email_confirmed_at,
                confirmed_at,
                created_at,
                updated_at,
                last_sign_in_at,
                raw_app_meta_data,
                raw_user_meta_data,
                phone,
                phone_confirmed_at,
                is_sso_user,
                is_anonymous,
                banned_until,
                deleted_at,
            ) = r
            e = norm_email(email)
            if not e:
                continue

            counts[e] = counts.get(e, 0) + 1
            if counts[e] > 1:
                raise SystemExit(
                    "Destination has multiple auth.users rows for the same normalized email. "
                    f"Email={e}. Resolve before syncing."
                )

            out[e] = {
                "id": uid,
                "email": email,
                "aud": aud,
                "role": role,
                "encrypted_password": encrypted_password,
                "email_confirmed_at": email_confirmed_at,
                "confirmed_at": confirmed_at,
                "created_at": created_at,
                "updated_at": updated_at,
                "last_sign_in_at": last_sign_in_at,
                "raw_app_meta_data": raw_app_meta_data,
                "raw_user_meta_data": raw_user_meta_data,
                "phone": phone,
                "phone_confirmed_at": phone_confirmed_at,
                "is_sso_user": is_sso_user,
                "is_anonymous": is_anonymous,
                "banned_until": banned_until,
                "deleted_at": deleted_at,
            }

    return out


def ensure_email_provider_in_raw_app_meta(raw_app: Any) -> Any:
    if raw_app is None:
        return {"provider": "email", "providers": ["email"]}
    if not isinstance(raw_app, dict):
        return raw_app
    raw_app = dict(raw_app)
    provs = raw_app.get("providers")
    if not isinstance(provs, list):
        provs = []
    provs2: list[str] = []
    for p in provs:
        if isinstance(p, str) and p not in provs2:
            provs2.append(p)
    if "email" not in provs2:
        provs2.append("email")
    raw_app["providers"] = provs2
    if not raw_app.get("provider"):
        raw_app["provider"] = "email"
    return raw_app


def fetch_src_identities(src_conn, *, user_ids: list[Any]) -> list[dict[str, Any]]:
    if not user_ids:
        return []

    user_ids = [str(x) for x in user_ids]

    rows = _fetchall(
        src_conn,
        """
        select provider_id, user_id, identity_data, provider, last_sign_in_at, created_at, updated_at
        from auth.identities
        where user_id = any(%s::uuid[])
        """,
        (user_ids,),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        provider_id, user_id, identity_data, provider, last_sign_in_at, created_at, updated_at = r
        out.append(
            {
                "id": uuid.uuid4(),
                "provider_id": provider_id,
                "user_id": user_id,
                "identity_data": identity_data,
                "provider": provider,
                "last_sign_in_at": last_sign_in_at,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )
    return out


def main() -> None:
    load_dotenv_if_available()

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--mode",
        choices=[
            "safe",
            "selected-idempotent",
            "selected-override",
            "all-idempotent",
            "all-override",
        ],
        default="safe",
    )
    ap.add_argument(
        "--emails", action="append", help="Comma-separated list (repeatable). For selected-* modes."
    )
    ap.add_argument("--emails-file", help="File with 1 email per line. For selected-* modes.")

    ap.add_argument("--apply", action="store_true", help="Write changes to destination.")
    ap.add_argument(
        "--yes-apply", action="store_true", help="Required along with --apply (2-key confirmation)."
    )

    ap.add_argument("--batch-size", type=int, default=200)
    ap.add_argument("--lookup-chunk-size", type=int, default=500)
    ap.add_argument("--debug", action="store_true")

    ap.add_argument("--promote-confirmation", action="store_true")
    ap.add_argument("--add-email-provider", action="store_true")
    ap.add_argument("--copy-identities", action="store_true")
    ap.add_argument("--allow-null-password", action="store_true")
    ap.add_argument(
        "--skip-oauth-only-overrides",
        action="store_true",
        help="If overriding and dst user looks OAuth-only (no 'email' in providers), skip override (extra safety).",
    )

    ap.add_argument("--no-guard", action="store_true")
    ap.add_argument("--yes-really", action="store_true")
    ap.add_argument("--allowlist", default=None)

    ap.add_argument(
        "--dst-instance-id",
        default=None,
        help="Optional explicit destination auth.instances.id to use when inserting users (only if auth.instances has multiple rows).",
    )

    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    if args.no_guard and not args.yes_really:
        raise SystemExit("--no-guard requires --yes-really")

    if args.apply and not args.yes_apply:
        raise SystemExit("--apply requires --yes-apply (two-key confirmation)")

    allowlist = set(ALLOWED_EMAILS_DEFAULT)
    if args.allowlist:
        allowlist = set()
        for line in Path(args.allowlist).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                allowlist.add(norm_email(line))
        if not allowlist:
            raise SystemExit("Allowlist file is empty after parsing.")

    autobots_db = env_optional("AUTOBOTS_DB_URL")
    bolt_db = env_optional("BOLT_DB_URL")
    if not autobots_db or not bolt_db:
        raise SystemExit(
            "Missing AUTOBOTS_DB_URL or BOLT_DB_URL in scripts/supabase_audit/.env.supabase_audit"
        )

    selected_mode = args.mode in ("safe", "selected-idempotent", "selected-override")
    all_mode = args.mode in ("all-idempotent", "all-override")
    override_existing = args.mode in ("selected-override", "all-override")

    if args.mode == "safe":
        override_existing = False

    selected_emails = load_selected_emails(args) if selected_mode else []
    if args.mode == "safe" and not selected_emails:
        selected_emails = sorted(allowlist)

    if selected_mode:
        selected_emails = enforce_allowlist(selected_emails, allowlist, no_guard=args.no_guard)
        if not selected_emails:
            raise SystemExit("After guardrail filtering, no emails remain to process.")

    src_conn = _connect(autobots_db, readonly=True)
    dst_conn = _connect(bolt_db, readonly=not args.apply)
    try:
        src_info = preflight(src_conn, label="src", want_write=False, dst_instance_id_override=None)
        dst_info = preflight(
            dst_conn,
            label="dst",
            want_write=bool(args.apply),
            dst_instance_id_override=args.dst_instance_id,
        )

        if src_info.rls_users and not src_info.rolbypassrls:
            LOG.info(
                "[src] RLS enabled on auth.users and bypassrls=false. Reads work only if explicit SELECT policies exist."
            )

        if all_mode:
            src_by_email = fetch_src_users(src_conn, all_mode=True, emails=[])
            src_emails = sorted(src_by_email.keys())
            if not args.no_guard:
                src_emails = enforce_allowlist(src_emails, allowlist, no_guard=False)
                src_by_email = {e: src_by_email[e] for e in src_emails if e in src_by_email}
        else:
            src_by_email = fetch_src_users(src_conn, all_mode=False, emails=selected_emails)
            src_emails = sorted(src_by_email.keys())

        dst_by_email = fetch_dst_users_chunked(
            dst_conn, emails=src_emails, chunk_size=int(args.lookup_chunk_size)
        )

        to_insert: list[str] = []
        to_update: list[str] = []
        to_skip: list[str] = []

        for e in src_emails:
            su = src_by_email[e]
            du = dst_by_email.get(e)

            if du is None:
                to_insert.append(e)
                continue

            if not override_existing:
                to_skip.append(e)
                continue

            if args.skip_oauth_only_overrides:
                raw_app = du.get("raw_app_meta_data") or {}
                provs = raw_app.get("providers") if isinstance(raw_app, dict) else None
                if (
                    isinstance(provs, list)
                    and "email" not in provs
                    and any(p in provs for p in ("google", "azure"))
                ):
                    LOG.warning("Skipping override for %s (dst appears OAuth-only).", e)
                    to_skip.append(e)
                    continue

            src_pw = su.get("encrypted_password")
            dst_pw = du.get("encrypted_password")
            if src_pw is None and not args.allow_null_password:
                LOG.warning("Override blocked for %s: source encrypted_password is NULL", e)
                to_skip.append(e)
                continue

            if src_pw != dst_pw:
                to_update.append(e)
            else:
                to_skip.append(e)

        LOG.info("=== PLAN ===")
        LOG.info(
            "mode=%s apply=%s override_existing=%s", args.mode, bool(args.apply), override_existing
        )
        LOG.info("source_candidates=%d destination_matched=%d", len(src_emails), len(dst_by_email))
        LOG.info("insert=%d update=%d skip=%d", len(to_insert), len(to_update), len(to_skip))

        sample_emails: list[str] = []
        if to_insert:
            sample_emails.append(to_insert[0])
        if to_update and len(sample_emails) < 2:
            sample_emails.append(to_update[0])

        for e in sample_emails:
            LOG.info("[sample:%s] src=%s", e, summarize_user_row(src_by_email[e]))
            if e in dst_by_email:
                LOG.info("[sample:%s] dst=%s", e, summarize_user_row(dst_by_email[e]))
            else:
                LOG.info("[sample:%s] dst=<missing>", e)

        report: dict[str, Any] = {
            "ts": utcnow_iso(),
            "mode": args.mode,
            "apply": bool(args.apply),
            "override_existing": override_existing,
            "guardrail_enabled": not args.no_guard,
            "allowlist": sorted(list(allowlist)) if not args.no_guard else None,
            "counts": {
                "source_candidates": len(src_emails),
                "dest_matched": len(dst_by_email),
                "insert": len(to_insert),
                "update": len(to_update),
                "skip": len(to_skip),
            },
            "insert_emails": to_insert,
            "update_emails": to_update,
            "skip_emails": to_skip,
            "db": {
                "src": {
                    "current_user": src_info.current_user,
                    "bypassrls": src_info.rolbypassrls,
                    "rls_auth_users": src_info.rls_users,
                    "users_count": src_info.users_count,
                },
                "dst": {
                    "current_user": dst_info.current_user,
                    "bypassrls": dst_info.rolbypassrls,
                    "rls_auth_users": dst_info.rls_users,
                    "users_count": dst_info.users_count,
                    "has_insert_users": dst_info.has_insert_users,
                    "has_update_users": dst_info.has_update_users,
                    "has_insert_identities": dst_info.has_insert_identities,
                    "instances_count": dst_info.instances_count,
                    "dst_instance_id": dst_info.dst_instance_id,
                    "users_instance_id_nullable": dst_info.users_instance_id_nullable,
                },
            },
            "options": {
                "promote_confirmation": bool(args.promote_confirmation),
                "add_email_provider": bool(args.add_email_provider),
                "copy_identities": bool(args.copy_identities),
                "allow_null_password": bool(args.allow_null_password),
                "skip_oauth_only_overrides": bool(args.skip_oauth_only_overrides),
                "batch_size": int(args.batch_size),
                "lookup_chunk_size": int(args.lookup_chunk_size),
            },
        }

        out_path = (
            args.report
            or f"tmp/sync_report_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        )

        if not args.apply:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_text(
                json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
            )
            LOG.info("Dry-run only. Wrote %s", out_path)
            return

        if to_insert and not dst_info.has_insert_users:
            raise SystemExit("Destination role lacks INSERT on auth.users.")
        if to_update and not dst_info.has_update_users:
            raise SystemExit("Destination role lacks UPDATE on auth.users.")
        if args.copy_identities and to_insert and not dst_info.has_insert_identities:
            raise SystemExit("Destination role lacks INSERT on auth.identities.")

        dst_conn.rollback()
        dst_conn.autocommit = False

        applied_inserts: list[str] = []
        applied_updates: list[str] = []
        identities_inserted = 0

        def _normalize_dst_user_row(cur, user_id: Any) -> None:
            null_to_empty = dst_info.users_null_to_empty_cols or []
            empty_to_null = dst_info.users_empty_to_null_cols or []
            if not null_to_empty and not empty_to_null:
                return

            from psycopg2 import sql as psql  # type: ignore

            assigns = []
            assigns += [
                psql.SQL("{} = COALESCE({}, '')").format(psql.Identifier(c), psql.Identifier(c))
                for c in null_to_empty
            ]
            assigns += [
                psql.SQL("{} = NULLIF({}, '')").format(psql.Identifier(c), psql.Identifier(c))
                for c in empty_to_null
            ]
            q = (
                psql.SQL("update auth.users set ")
                + psql.SQL(", ").join(assigns)
                + psql.SQL(" where id = %s")
            )
            cur.execute(q, (user_id,))

        def apply_batch(batch_emails: list[str]) -> None:
            nonlocal identities_inserted
            inserted_in_this_batch: list[str] = []

            cur = dst_conn.cursor()
            try:
                for e in batch_emails:
                    su = src_by_email[e]
                    du = dst_by_email.get(e)

                    if du is None and e in to_insert:
                        phone_val = su.get("phone")
                        if isinstance(phone_val, str) and phone_val.strip() == "":
                            phone_val = None

                        id_row = _fetchone(
                            dst_conn, "select email from auth.users where id=%s", (su["id"],)
                        )
                        if id_row:
                            existing_email = norm_email(id_row[0] or "")
                            if existing_email and existing_email != e:
                                raise SystemExit(
                                    f"Refusing insert for {e}: source id {su['id']} already exists in destination "
                                    f"bound to email={existing_email}."
                                )
                            LOG.warning(
                                "Destination already has id=%s for email=%s; skipping insert.",
                                su["id"],
                                e,
                            )
                            continue

                        cur.execute(
                            """
                            insert into auth.users (
                              instance_id, id, aud, role, email, encrypted_password,
                              email_confirmed_at,
                              created_at, updated_at, last_sign_in_at,
                              raw_app_meta_data, raw_user_meta_data,
                              phone, phone_confirmed_at,
                              is_sso_user, is_anonymous,
                              banned_until, deleted_at
                            ) values (
                              %s,%s,%s,%s,%s,%s,
                              %s,
                              %s,%s,%s,
                              %s,%s,
                              %s,%s,
                              %s,%s,
                              %s,%s
                            )
                            """,
                            (
                                dst_info.dst_instance_id,
                                su["id"],
                                su.get("aud"),
                                su.get("role"),
                                su.get("email"),
                                su.get("encrypted_password"),
                                su.get("email_confirmed_at"),
                                su.get("created_at"),
                                su.get("updated_at"),
                                su.get("last_sign_in_at"),
                                _json_param(su.get("raw_app_meta_data")),
                                _json_param(su.get("raw_user_meta_data")),
                                phone_val,
                                su.get("phone_confirmed_at"),
                                su.get("is_sso_user"),
                                su.get("is_anonymous"),
                                su.get("banned_until"),
                                su.get("deleted_at"),
                            ),
                        )
                        applied_inserts.append(e)
                        inserted_in_this_batch.append(e)

                        _normalize_dst_user_row(cur, su["id"])

                    elif du is not None and override_existing and e in to_update:
                        src_pw = su.get("encrypted_password")
                        if src_pw is None and not args.allow_null_password:
                            continue

                        email_confirmed_at = du.get("email_confirmed_at")
                        if args.promote_confirmation:
                            email_confirmed_at = email_confirmed_at or su.get("email_confirmed_at")

                        raw_app = du.get("raw_app_meta_data")
                        if args.add_email_provider:
                            raw_app = ensure_email_provider_in_raw_app_meta(raw_app)

                        cur.execute(
                            """
                            update auth.users
                            set encrypted_password = %s,
                                email_confirmed_at = %s,
                                raw_app_meta_data = %s,
                                updated_at = now()
                            where id = %s
                            """,
                            (src_pw, email_confirmed_at, _json_param(raw_app), du["id"]),
                        )
                        applied_updates.append(e)

                if args.copy_identities and inserted_in_this_batch:
                    inserted_ids = [src_by_email[e]["id"] for e in inserted_in_this_batch]
                    idents = fetch_src_identities(src_conn, user_ids=inserted_ids)
                    for ident in idents:
                        cur.execute(
                            """
                            insert into auth.identities (
                              id, provider_id, user_id, identity_data, provider,
                              last_sign_in_at, created_at, updated_at
                            ) values (
                              %s,%s,%s,%s,%s,
                              %s,%s,%s
                            )
                            on conflict (provider_id, provider) do nothing
                            """,
                            (
                                ident["id"],
                                ident["provider_id"],
                                ident["user_id"],
                                _json_param(ident["identity_data"]),
                                ident["provider"],
                                ident["last_sign_in_at"],
                                ident["created_at"],
                                ident["updated_at"],
                            ),
                        )
                    identities_inserted += len(idents)

                dst_conn.commit()
            except Exception:
                dst_conn.rollback()
                raise
            finally:
                cur.close()

        planned = [e for e in src_emails if e in to_insert or e in to_update]
        for batch in chunks(planned, max(1, int(args.batch_size))):
            LOG.info("Applying batch: %d emails", len(batch))
            apply_batch(batch)

        report["applied"] = {
            "insert_emails": applied_inserts,
            "update_emails": applied_updates,
            "identities_inserted": identities_inserted,
        }

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(
            json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
        )
        LOG.info("Apply complete. Wrote %s", out_path)

    finally:
        src_conn.close()
        dst_conn.close()


if __name__ == "__main__":
    main()
