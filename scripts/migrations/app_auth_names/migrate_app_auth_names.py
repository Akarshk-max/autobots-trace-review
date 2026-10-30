#!/usr/bin/env python3
"""Backfill ``name`` and ``default`` on legacy AppAuth / AppCreds documents.

The ``name`` (str, required) and ``default`` (bool, default True) fields were
introduced in AUT-1317.  Documents created before that release have neither
field, which breaks the current read paths:

* ``IntegrationDoc`` / ``AppCredsDoc`` model validation raises ``ValidationError``
  because ``name`` is required with no default value.
* ``get_by_app_name(default=True)`` queries miss legacy docs because MongoDB
  does not match ``{default: true}`` against a missing field.
* ``get_by_name(name=...)`` queries miss legacy docs because ``name`` is absent.

Unlike ``lookup`` (which has a two-step legacy fallback in
``get_by_slack_user``), there is **no** fallback for ``name`` or ``default``.
A data backfill is the only way to make legacy docs work with the current code.

This script:

1. Runs a diagnostic pass (counts + duplicate check) — always, even with
   ``--apply``.
2. If ``--apply`` is set, prompts for confirmation, then runs the backfills.
3. If duplicate ``(user_id, space_id, app_name, name)`` groups are found in
   ``AppCreds``, optionally dedupes them (with explicit confirmation).

The backfill is purely additive — it only sets fields that were missing.  It
does not touch ``lookup``, ``auth``, ``creds``, or any existing ``name`` /
``default`` values.

Usage
-----
    # Dry-run for a single user (diagnostic only, no writes)
    uv run python scripts/migrations/app_auth_names/migrate_app_auth_names.py \
        --user e50e84de-75d1-41ec-b1bc-aec5d7140833

    # Apply for a single user
    uv run python scripts/migrations/app_auth_names/migrate_app_auth_names.py \
        --user e50e84de-75d1-41ec-b1bc-aec5d7140833 --apply

    # Dry-run for all users
    uv run python scripts/migrations/app_auth_names/migrate_app_auth_names.py \
        --user all

    # Apply for all users
    uv run python scripts/migrations/app_auth_names/migrate_app_auth_names.py \
        --user all --apply
"""

import argparse
import asyncio
from pathlib import Path
from typing import Any

from bson import ObjectId
from loguru import logger

APP_AUTH_COLLECTION = "AppAuth"
APP_CREDS_COLLECTION = "AppCreds"

# Filters for legacy docs ----------------------------------------------------
# ``name`` is missing, null, or empty string.  ``$in`` with ``None`` matches
# both ``null`` values and missing fields in MongoDB.
NAME_MISSING = {"name": {"$in": [None, ""]}}

# ``default`` is missing or null.  Does NOT match ``default: false`` (explicit
# false is respected, matching the runtime normalizer behaviour).
DEFAULT_MISSING = {"default": None}

# ``app_name`` must be present and non-empty for the name backfill to work
# (``name = $app_name``).  Docs without ``app_name`` are reported but skipped.
APP_NAME_PRESENT = {"app_name": {"$exists": True, "$nin": [None, ""]}}

# Dedup suffix for orphaned AppCreds duplicates.
DEDUP_SUFFIX = "__legacy__"


def _ensure_repo_root_on_path() -> None:
    import sys

    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backfill name/default on legacy AppAuth & AppCreds documents.",
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
    """Run read-only counts and duplicate checks.  Returns a summary dict."""
    uf = _user_filter(user_arg)
    summary: dict[str, Any] = {}

    for coll_name in (APP_AUTH_COLLECTION, APP_CREDS_COLLECTION):
        coll = db[coll_name]

        name_missing = await coll.count_documents({**uf, **NAME_MISSING})
        default_missing = await coll.count_documents({**uf, **DEFAULT_MISSING})
        # Docs where name is missing AND app_name is also missing — these
        # cannot be backfilled (name = $app_name would set name = null).
        both_missing = await coll.count_documents(
            {**uf, **NAME_MISSING, "app_name": {"$in": [None, ""]}}
        )
        # Docs where app_name exists but name is missing — these CAN be
        # backfilled.
        name_backfillable = await coll.count_documents({**uf, **NAME_MISSING, **APP_NAME_PRESENT})

        summary[coll_name] = {
            "name_missing": name_missing,
            "default_missing": default_missing,
            "both_missing": both_missing,
            "name_backfillable": name_backfillable,
        }

    # AppCreds duplicate check: group by (user_id, space_id, app_name) and
    # report groups with more than one doc.
    dup_groups = await _find_app_creds_duplicates(db, uf)
    summary["app_creds_dup_groups"] = dup_groups

    return summary


async def _find_app_creds_duplicates(
    db: Any,
    user_filter: dict[str, Any],
) -> list[dict[str, Any]]:
    """Find (user_id, space_id, app_name, name) groups with >1 AppCreds doc.

    Groups by ``(user_id, space_id, app_name, name)`` — the same tuple the
    runtime create flow checks for existing docs.  This means:

    * Before dedup: legacy docs all have ``name = app_name`` (after the name
      backfill runs), so they collapse into one group and are detected.
    * After dedup: the winner keeps ``name = app_name`` while losers get
      ``name = "<app_name>__legacy__<suffix>"``, so they split into separate
      groups and are no longer reported as duplicates on re-run.

    Docs without ``app_name`` are excluded — they cannot be deduped safely
    (there is no value to copy into ``name``) and are reported separately in
    the diagnostic.
    """
    coll = db[APP_CREDS_COLLECTION]
    pipeline = [
        {"$match": {**user_filter, **APP_NAME_PRESENT}},
        {
            "$group": {
                "_id": {
                    "user_id": "$user_id",
                    "space_id": "$space_id",
                    "app_name": "$app_name",
                    "name": "$name",
                },
                "count": {"$sum": 1},
                "doc_ids": {"$push": "$_id"},
            }
        },
        {"$match": {"count": {"$gt": 1}}},
    ]
    groups: list[dict[str, Any]] = []
    async for g in coll.aggregate(pipeline):
        groups.append(
            {
                "user_id": g["_id"].get("user_id"),
                "space_id": g["_id"].get("space_id"),
                "app_name": g["_id"].get("app_name"),
                "name": g["_id"].get("name"),
                "count": g["count"],
                "doc_ids": [str(did) for did in g["doc_ids"]],
            }
        )
    return groups


def _print_diagnostic(summary: dict[str, Any], user_arg: str) -> None:
    print()
    print("=" * 72)
    print(f"  DIAGNOSTIC REPORT  (scope: {'ALL USERS' if user_arg == 'all' else user_arg})")
    print("=" * 72)

    for coll_name in (APP_AUTH_COLLECTION, APP_CREDS_COLLECTION):
        s = summary[coll_name]
        print(f"\n  {coll_name}:")
        print(f"    docs missing name (null/empty/absent) : {s['name_missing']}")
        print(f"    docs missing default (null/absent)    : {s['default_missing']}")
        print(f"    docs backfillable (name <- app_name)  : {s['name_backfillable']}")
        print(f"    docs where app_name ALSO missing      : {s['both_missing']}")
        if s["both_missing"]:
            print("      ^ these will be SKIPPED (cannot derive name)")

    dup_groups = summary["app_creds_dup_groups"]
    if dup_groups:
        print(f"\n  AppCreds DUPLICATE GROUPS ({len(dup_groups)}):")
        for g in dup_groups:
            print(
                f"    user={g['user_id']}  space={g['space_id']}  "
                f"app={g['app_name']}  name={g['name']}  count={g['count']}  "
                f"ids={g['doc_ids']}"
            )
        print("  ^ dedup available with --apply (will prompt for confirmation)")
    else:
        print("\n  AppCreds duplicate groups: 0 (no dedup needed)")

    print()
    print("=" * 72)


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


async def _backfill_name(coll: Any, user_filter: dict[str, Any]) -> dict[str, int]:
    """Set name = app_name on docs missing name (where app_name is present).

    Uses aggregation-pipeline form to reference the doc's own ``app_name``
    field.  Requires Mongo 4.2+ (Atlas 7.0.40 is fine).
    """
    result = await coll.update_many(
        {**user_filter, **NAME_MISSING, **APP_NAME_PRESENT},
        [{"$set": {"name": "$app_name"}}],
    )
    return {
        "matched": result.matched_count,
        "modified": result.modified_count,
    }


async def _backfill_default(coll: Any, user_filter: dict[str, Any]) -> dict[str, int]:
    """Set default = true on docs missing default (null or absent).

    Does NOT touch docs with ``default: false`` (explicit false is respected).
    """
    result = await coll.update_many(
        {**user_filter, **DEFAULT_MISSING},
        {"$set": {"default": True}},
    )
    return {
        "matched": result.matched_count,
        "modified": result.modified_count,
    }


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


async def _dedup_app_creds(
    db: Any,
    dup_groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """For each duplicate group, keep one winner and rename the rest.

    Winner selection:
      1. Prefer a doc with ``default: true``.
      2. Otherwise, the most recently ``updated_at``.

    Winner:  name = app_name, default = true
    Losers:  name = "<app_name>__legacy__<last 8 hex of _id>", default = false
    """
    coll = db[APP_CREDS_COLLECTION]
    results: list[dict[str, Any]] = []

    for g in dup_groups:
        doc_ids = g["doc_ids"]
        # Fetch all docs in the group to pick a winner.
        docs = []
        async for doc in coll.find({"_id": {"$in": [ObjectId(did) for did in doc_ids]}}):
            docs.append(doc)

        if not docs:
            continue

        # Winner: prefer default:true, then newest updated_at.
        winner = max(
            docs,
            key=lambda d: (
                d.get("default") is True,
                d.get("updated_at") or d.get("created_at"),
            ),
        )
        winner_id = winner["_id"]
        app_name = g["app_name"]

        # Update winner.
        await coll.update_one(
            {"_id": winner_id},
            {"$set": {"name": app_name, "default": True}},
        )

        # Rename losers.
        loser_results = []
        for doc in docs:
            if doc["_id"] == winner_id:
                continue
            suffix = str(doc["_id"])[-8:]
            legacy_name = f"{app_name}{DEDUP_SUFFIX}{suffix}"
            await coll.update_one(
                {"_id": doc["_id"]},
                {"$set": {"name": legacy_name, "default": False}},
            )
            loser_results.append({"id": str(doc["_id"]), "name": legacy_name})

        results.append(
            {
                "group": (
                    f"user={g['user_id']} space={g['space_id']} app={app_name} name={g['name']}"
                ),
                "winner": str(winner_id),
                "losers": loser_results,
            }
        )

    return results


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

        logger.info(f"Starting name/default backfill migration: user={user_arg} apply={args.apply}")

        # 1. Diagnostic (always runs)
        summary = await _diagnose(db, user_arg)
        _print_diagnostic(summary, user_arg)

        # 2. Check if there's anything to do
        total_name = sum(
            s["name_backfillable"]
            for s in [summary[APP_AUTH_COLLECTION], summary[APP_CREDS_COLLECTION]]
        )
        total_default = sum(
            s["default_missing"]
            for s in [summary[APP_AUTH_COLLECTION], summary[APP_CREDS_COLLECTION]]
        )
        dup_groups = summary["app_creds_dup_groups"]

        if total_name == 0 and total_default == 0 and not dup_groups:
            print("  Nothing to backfill.  All docs already have name and default.")
            return

        if not args.apply:
            print("  Dry run complete.  Re-run with --apply to write changes.")
            return

        # 3. Confirmation
        if not args.yes:
            print(f"  About to WRITE to MongoDB (scope: {user_arg}):")
            print(
                f"    AppAuth:  backfill name on {summary[APP_AUTH_COLLECTION]['name_backfillable']} doc(s)"
            )
            print(
                f"    AppAuth:  backfill default on {summary[APP_AUTH_COLLECTION]['default_missing']} doc(s)"
            )
            print(
                f"    AppCreds: backfill name on {summary[APP_CREDS_COLLECTION]['name_backfillable']} doc(s)"
            )
            print(
                f"    AppCreds: backfill default on {summary[APP_CREDS_COLLECTION]['default_missing']} doc(s)"
            )
            if dup_groups:
                print(f"    AppCreds: {len(dup_groups)} duplicate group(s) found")
            print()
            answer = input("  Proceed? [y/N] ").strip().lower()
            if answer != "y":
                print("  Aborted.  No changes made.")
                return

            # 3a. Dedup confirmation (separate from backfill)
            do_dedup = False
            if dup_groups:
                print()
                print(f"  Found {len(dup_groups)} AppCreds duplicate group(s).")
                print("  Dedup will keep one winner per group and rename the rest")
                print("  to '<app_name>__legacy__<id suffix>' with default=false.")
                answer = input("  Run dedup? [y/N] ").strip().lower()
                do_dedup = answer == "y"
        else:
            do_dedup = bool(dup_groups)

        # 4. Backfill name
        print()
        print("  --- Backfilling name ---")
        for coll_name in (APP_AUTH_COLLECTION, APP_CREDS_COLLECTION):
            r = await _backfill_name(db[coll_name], uf)
            print(f"    {coll_name}: matched={r['matched']} modified={r['modified']}")

        # 5. Backfill default
        print("  --- Backfilling default ---")
        for coll_name in (APP_AUTH_COLLECTION, APP_CREDS_COLLECTION):
            r = await _backfill_default(db[coll_name], uf)
            print(f"    {coll_name}: matched={r['matched']} modified={r['modified']}")

        # 6. Dedup (optional)
        if do_dedup and dup_groups:
            print("  --- Deduping AppCreds ---")
            results = await _dedup_app_creds(db, dup_groups)
            for r in results:
                print(f"    {r['group']}")
                print(f"      winner: {r['winner']}")
                for loser in r["losers"]:
                    print(f"      loser:  {loser['id']} -> name='{loser['name']}'")
        elif dup_groups and not do_dedup:
            print("  --- Skipping AppCreds dedup (not confirmed) ---")

        print()
        print("  Migration complete.")
    finally:
        close_mongo_client()


if __name__ == "__main__":
    asyncio.run(main())
