#!/usr/bin/env python3
"""Backfill ``lookup`` on legacy Slack AppAuth documents.

The ``lookup`` field (a plaintext dict holding ``authed_user_id`` and
``team_id`` for Slack apps) was introduced so that Slack identity can be
queried without decrypting ``auth`` (which is encrypted at rest — see
``extract_lookup`` in ``app_auth_lookup.py`` and the create flow in
``user_app_auth.py`` where ``extract_lookup`` runs on plaintext ``auth``
*before* ``encrypt_if_needed``).

Documents created before ``lookup`` existed have no ``lookup`` field.  The
runtime has a two-step fallback in ``get_by_slack_user`` /
``get_user_by_slack_user`` that retries against the legacy top-level fields
``slack_authed_user_id`` / ``slack_team_id``.  But that fallback only helps
docs that actually carry those top-level fields.  Many legacy docs store
the Slack identity *only* inside ``auth.authed_user.id`` / ``auth.team.id``
— for those, both the primary ``lookup.*`` query and the legacy top-level
fallback miss, and the webhook returns 404 / 403.

This script backfills ``lookup`` from whichever source is available on each
legacy Slack AppAuth doc, in priority order:

* **Pass 1** — top-level legacy fields ``slack_authed_user_id`` /
  ``slack_team_id`` (oldest docs).
* **Pass 2a** — plaintext ``auth.authed_user.id`` + ``auth.team.id``
  (docs with both nested fields).
* **Pass 2b** — plaintext ``auth.authed_user.id`` only (docs whose
  ``auth.team`` is missing/null — matches ``extract_lookup``'s
  ``exclude_none=True`` behaviour, which omits ``team_id`` when absent).

This script is intended to run **before** the ``auth`` encryption migration.
At that point every doc's ``auth`` is plaintext, so Pass 2 can read
``auth.authed_user.id`` / ``auth.team.id`` directly via an aggregation
pipeline — no decryption, no per-doc iteration.

Scope:

* **Collection:** ``AppAuth`` only.  ``AppCreds`` has no ``lookup`` field.
* **Apps:** ``slack`` and ``slack_user`` only — the only apps
  ``extract_lookup`` populates ``lookup`` for.
* **Condition:** ``lookup`` is missing, null, or empty ``{}``.

The backfill is purely additive — it only sets ``lookup`` where it was
missing/empty.  It does not touch ``auth``, ``creds``, ``name``, ``default``,
or the legacy top-level fields.  It is idempotent: re-running is a no-op
because backfilled docs have non-empty ``lookup`` and fall outside the
``LOOKUP_MISSING`` filter.

Usage
-----
    # Dry-run for a single user (diagnostic only, no writes)
    uv run python scripts/migrations/app_auth_lookup/migrate_app_auth_lookup.py \
        --user e50e84de-75d1-41ec-b1bc-aec5d7140833

    # Apply for a single user
    uv run python scripts/migrations/app_auth_lookup/migrate_app_auth_lookup.py \
        --user e50e84de-75d1-41ec-b1bc-aec5d7140833 --apply

    # Dry-run for all users
    uv run python scripts/migrations/app_auth_lookup/migrate_app_auth_lookup.py \
        --user all

    # Apply for all users
    uv run python scripts/migrations/app_auth_lookup/migrate_app_auth_lookup.py \
        --user all --apply
"""

import argparse
import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

APP_AUTH_COLLECTION = "AppAuth"

# Only Slack apps have a meaningful ``lookup``.  ``extract_lookup`` returns
# ``{}`` for every other app, so there is nothing to backfill for them.
SLACK_APPS = {"app_name": {"$in": ["slack", "slack_user"]}}

# ``lookup`` is missing, null, or empty dict.  ``$in`` with ``None`` matches
# both ``null`` values and missing fields in MongoDB; ``{}`` matches docs
# where ``extract_lookup`` already wrote an empty dict (non-Slack apps, or
# Slack docs whose ``auth`` had no identity — those are skipped by the
# source-present filters below).
LOOKUP_MISSING = {"lookup": {"$in": [None, {}]}}

# Pass 1 source: top-level legacy fields present.
SLACK_USER_ID_TOPLEVEL_PRESENT = {
    "slack_authed_user_id": {"$exists": True, "$nin": [None, ""]},
}
SLACK_TEAM_ID_TOPLEVEL_PRESENT = {
    "slack_team_id": {"$exists": True, "$nin": [None, ""]},
}

# Pass 2 source: identity inside plaintext ``auth``.  We match on the nested
# fields existing and being non-empty strings.  Encrypted ``auth`` has the
# shape ``{"_enc": True, "data": "...", ...}`` with no ``authed_user`` /
# ``team`` keys, so these filters naturally exclude encrypted docs.  (This
# script is intended to run before the encryption migration, so all docs
# should be plaintext — but the filter is defensive either way.)
AUTH_USER_ID_PRESENT = {
    "auth.authed_user.id": {"$exists": True, "$nin": [None, ""]},
}
AUTH_TEAM_ID_PRESENT = {
    "auth.team.id": {"$exists": True, "$nin": [None, ""]},
}
AUTH_TEAM_ID_ABSENT = {
    "auth.team.id": {"$in": [None, ""]},  # matches null OR missing
}


def _ensure_repo_root_on_path() -> None:
    import sys

    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backfill lookup on legacy Slack AppAuth documents.",
    )
    p.add_argument(
        "--user",
        required=True,
        help=(
            "User ID (UUID) to migrate, or the literal string 'all' for every "
            "user.  Required — there is no default.  Fail-closed to prevent "
            "accidental full-collection runs."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually write updates.  Without this flag, runs in dry-run mode.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts.  Use with caution (intended for CI).",
    )
    return p.parse_args()


def _user_filter(user_arg: str) -> dict[str, Any]:
    """Return a Mongo filter dict scoped to the user, or empty for 'all'."""
    if user_arg == "all":
        return {}
    return {"user_id": user_arg}


# ---------------------------------------------------------------------------
# Diagnostic
# ---------------------------------------------------------------------------


async def _diagnose(
    db: Any,
    user_arg: str,
) -> dict[str, Any]:
    """Run read-only counts.  Returns a summary dict."""
    uf = _user_filter(user_arg)
    coll = db[APP_AUTH_COLLECTION]

    base = {**uf, **SLACK_APPS, **LOOKUP_MISSING}

    lookup_missing = await coll.count_documents(base)

    # Pass 1: top-level legacy fields present.
    pass1_both = await coll.count_documents(
        {**base, **SLACK_USER_ID_TOPLEVEL_PRESENT, **SLACK_TEAM_ID_TOPLEVEL_PRESENT}
    )
    pass1_user_only = await coll.count_documents(
        {**base, **SLACK_USER_ID_TOPLEVEL_PRESENT, "slack_team_id": {"$in": [None, ""]}}
    )

    # Pass 2: identity inside plaintext auth.  Exclude docs already covered
    # by Pass 1 (top-level fields present) so counts don't double-count.
    not_pass1 = {"slack_authed_user_id": {"$in": [None, ""]}}
    pass2_both = await coll.count_documents(
        {**base, **not_pass1, **AUTH_USER_ID_PRESENT, **AUTH_TEAM_ID_PRESENT}
    )
    pass2_user_only = await coll.count_documents(
        {**base, **not_pass1, **AUTH_USER_ID_PRESENT, **AUTH_TEAM_ID_ABSENT}
    )

    # Docs where no source is available — cannot be backfilled.
    no_source = await coll.count_documents(
        {
            **base,
            "slack_authed_user_id": {"$in": [None, ""]},
            "auth.authed_user.id": {"$in": [None, ""]},
        }
    )

    backfillable = pass1_both + pass1_user_only + pass2_both + pass2_user_only

    return {
        APP_AUTH_COLLECTION: {
            "lookup_missing": lookup_missing,
            "lookup_backfillable": backfillable,
            "pass1_both": pass1_both,
            "pass1_user_only": pass1_user_only,
            "pass2_both": pass2_both,
            "pass2_user_only": pass2_user_only,
            "no_source": no_source,
        }
    }


def _print_diagnostic(summary: dict[str, Any], user_arg: str) -> None:
    print()
    print("=" * 72)
    print(f"  DIAGNOSTIC REPORT  (scope: {'ALL USERS' if user_arg == 'all' else user_arg})")
    print("=" * 72)

    s = summary[APP_AUTH_COLLECTION]
    print(f"\n  {APP_AUTH_COLLECTION} (slack / slack_user only):")
    print(f"    docs missing lookup (null/empty/absent)      : {s['lookup_missing']}")
    print(f"    docs backfillable                            : {s['lookup_backfillable']}")
    print(f"      Pass 1  (top-level, both fields)           : {s['pass1_both']}")
    print(f"      Pass 1  (top-level, authed_user_id only)   : {s['pass1_user_only']}")
    print(f"      Pass 2a (auth, both fields)                : {s['pass2_both']}")
    print(f"      Pass 2b (auth, authed_user_id only)        : {s['pass2_user_only']}")
    print(f"    docs with NO source (cannot backfill)        : {s['no_source']}")
    if s["no_source"]:
        print("      ^ these will be SKIPPED (no identity found in any source)")

    print()
    print("=" * 72)


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


async def _backfill_pass1_both(coll: Any, user_filter: dict[str, Any]) -> dict[str, int]:
    """Pass 1: top-level legacy fields, both present.

    Sets ``lookup = {authed_user_id, team_id}`` from ``slack_authed_user_id``
    and ``slack_team_id``.
    """
    result = await coll.update_many(
        {
            **user_filter,
            **SLACK_APPS,
            **LOOKUP_MISSING,
            **SLACK_USER_ID_TOPLEVEL_PRESENT,
            **SLACK_TEAM_ID_TOPLEVEL_PRESENT,
        },
        [{"$set": {"lookup": {
            "authed_user_id": "$slack_authed_user_id",
            "team_id": "$slack_team_id",
        }}}],
    )
    return {"matched": result.matched_count, "modified": result.modified_count}


async def _backfill_pass1_user_only(coll: Any, user_filter: dict[str, Any]) -> dict[str, int]:
    """Pass 1: top-level ``slack_authed_user_id`` only (no ``slack_team_id``).

    Sets ``lookup = {authed_user_id}`` — omits ``team_id`` to match
    ``extract_lookup``'s ``exclude_none=True`` behaviour.
    """
    result = await coll.update_many(
        {
            **user_filter,
            **SLACK_APPS,
            **LOOKUP_MISSING,
            **SLACK_USER_ID_TOPLEVEL_PRESENT,
            "slack_team_id": {"$in": [None, ""]},
        },
        [{"$set": {"lookup": {
            "authed_user_id": "$slack_authed_user_id",
        }}}],
    )
    return {"matched": result.matched_count, "modified": result.modified_count}


async def _backfill_pass2_both(coll: Any, user_filter: dict[str, Any]) -> dict[str, int]:
    """Pass 2a: plaintext ``auth``, both ``authed_user.id`` and ``team.id``.

    Sets ``lookup = {authed_user_id, team_id}`` from
    ``auth.authed_user.id`` and ``auth.team.id``.  Excludes docs already
    covered by Pass 1 (top-level fields present).
    """
    result = await coll.update_many(
        {
            **user_filter,
            **SLACK_APPS,
            **LOOKUP_MISSING,
            "slack_authed_user_id": {"$in": [None, ""]},
            **AUTH_USER_ID_PRESENT,
            **AUTH_TEAM_ID_PRESENT,
        },
        [{"$set": {"lookup": {
            "authed_user_id": "$auth.authed_user.id",
            "team_id": "$auth.team.id",
        }}}],
    )
    return {"matched": result.matched_count, "modified": result.modified_count}


async def _backfill_pass2_user_only(coll: Any, user_filter: dict[str, Any]) -> dict[str, int]:
    """Pass 2b: plaintext ``auth``, ``authed_user.id`` only (no ``team.id``).

    Sets ``lookup = {authed_user_id}`` — omits ``team_id`` to match
    ``extract_lookup``'s ``exclude_none=True`` behaviour.  Excludes docs
    already covered by Pass 1.
    """
    result = await coll.update_many(
        {
            **user_filter,
            **SLACK_APPS,
            **LOOKUP_MISSING,
            "slack_authed_user_id": {"$in": [None, ""]},
            **AUTH_USER_ID_PRESENT,
            **AUTH_TEAM_ID_ABSENT,
        },
        [{"$set": {"lookup": {
            "authed_user_id": "$auth.authed_user.id",
        }}}],
    )
    return {"matched": result.matched_count, "modified": result.modified_count}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    _ensure_repo_root_on_path()
    args = _parse_args()

    from src.autobots.core.database.mongo_base import close_mongo_client, get_mongo_db

    db = next(get_mongo_db())
    try:
        user_arg = args.user
        uf = _user_filter(user_arg)

        logger.info(f"Starting lookup backfill migration: user={user_arg} apply={args.apply}")

        # 1. Diagnostic (always runs)
        summary = await _diagnose(db, user_arg)
        _print_diagnostic(summary, user_arg)

        s = summary[APP_AUTH_COLLECTION]

        # 2. Check if there's anything to do
        if s["lookup_backfillable"] == 0:
            if s["no_source"]:
                print(
                    f"  Nothing to backfill.  {s['no_source']} doc(s) have no "
                    f"source for lookup and will be skipped."
                )
            else:
                print("  Nothing to backfill.  All Slack AppAuth docs already have lookup.")
            return

        if not args.apply:
            print("  Dry run complete.  Re-run with --apply to write changes.")
            return

        # 3. Confirmation
        if not args.yes:
            print(f"  About to WRITE to MongoDB (scope: {user_arg}):")
            print(f"    AppAuth: backfill lookup on {s['lookup_backfillable']} doc(s)")
            print(f"      Pass 1  (top-level, both)         : {s['pass1_both']}")
            print(f"      Pass 1  (top-level, user only)    : {s['pass1_user_only']}")
            print(f"      Pass 2a (auth, both)              : {s['pass2_both']}")
            print(f"      Pass 2b (auth, user only)         : {s['pass2_user_only']}")
            if s["no_source"]:
                print(f"    (skipping {s['no_source']} doc(s) with no source)")
            print()
            answer = input("  Proceed? [y/N] ").strip().lower()
            if answer != "y":
                print("  Aborted.  No changes made.")
                return
        else:
            if s["no_source"]:
                print(f"  (skipping {s['no_source']} doc(s) with no source for lookup)")

        coll = db[APP_AUTH_COLLECTION]

        # 4. Pass 1 — top-level legacy fields
        print()
        print("  --- Pass 1: top-level legacy fields (both) ---")
        r = await _backfill_pass1_both(coll, uf)
        print(f"    matched={r['matched']} modified={r['modified']}")

        print("  --- Pass 1: top-level legacy fields (authed_user_id only) ---")
        r = await _backfill_pass1_user_only(coll, uf)
        print(f"    matched={r['matched']} modified={r['modified']}")

        # 5. Pass 2 — plaintext auth
        print("  --- Pass 2a: auth.authed_user.id + auth.team.id ---")
        r = await _backfill_pass2_both(coll, uf)
        print(f"    matched={r['matched']} modified={r['modified']}")

        print("  --- Pass 2b: auth.authed_user.id only ---")
        r = await _backfill_pass2_user_only(coll, uf)
        print(f"    matched={r['matched']} modified={r['modified']}")

        print()
        print("  Migration complete.")
    finally:
        close_mongo_client()


if __name__ == "__main__":
    asyncio.run(main())
