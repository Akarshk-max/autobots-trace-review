#!/usr/bin/env python3
"""Copy each connection's credentials onto the AppAuth document that holds its auth.

Credentials used to live in their own `AppCreds` collection, so one connection was two
documents that could disagree — and did, whenever one was written and the other was not.
`AppAuth` now carries a `creds` field, and `UserAppCreds` reads it first, falling back to
the old collection while this backfill has not run.

This copies the creds across. It does not delete anything: the `AppCreds` collection stays
readable, so the fallback still works and a bad run is undone by ignoring the new field.

A connection exists if either half of it does. Credentials with no auth document are still
a connection — the auth was simply never minted — so this creates the document for them,
carrying the creds and an empty `auth`. Absent means `{}`, never null: an empty dict is a
thing that is not there yet, and the code reads it that way.

Matching is on the four fields that identify a connection — user_id, space_id, app_name,
name — because that is the tuple `UserAppCreds.get_app_creds_by_name` looks up. Anything
that does not match exactly is reported, never guessed at.

Idempotent: a document that already carries creds is left alone, so a second run copies
nothing. Encrypted values are moved verbatim; nothing is decrypted or re-encrypted here.

Usage
-----
    python -m scripts.migrations.fold_app_creds_into_app_auth
    python -m scripts.migrations.fold_app_creds_into_app_auth --apply
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
from typing import Any, Dict

from src.autobots.core.database.mongo_base import get_mongo_client
from src.autobots.core.settings import SettingsProvider

CREDS_COLLECTION = "AppCreds"
AUTH_COLLECTION = "AppAuth"


def _identity(doc: Dict[str, Any]) -> tuple:
    """The four fields that make one connection, in the order the app looks them up."""
    return (
        doc.get("user_id"),
        doc.get("space_id"),
        doc.get("app_name"),
        doc.get("name"),
    )


async def main(apply: bool) -> None:
    client = get_mongo_client()
    db = client[SettingsProvider.sget().MONGO_DATABASE]

    auth_by_identity: Dict[tuple, Dict[str, Any]] = {}
    duplicate_auth: list[tuple] = []
    async for doc in db[AUTH_COLLECTION].find({}):
        key = _identity(doc)
        if key in auth_by_identity:
            duplicate_auth.append(key)
            continue
        auth_by_identity[key] = doc

    scanned = folded = already = created = 0
    async for creds_doc in db[CREDS_COLLECTION].find({}):
        scanned += 1
        creds = creds_doc.get("creds")
        if not creds:
            continue

        key = _identity(creds_doc)
        auth_doc = auth_by_identity.get(key)
        if not auth_doc:
            # Credentials with no auth document. The auth was never minted — the user
            # saved credentials and never connected, or the connection was removed and
            # these outlived it. It is still a connection, so it gets a document.
            created += 1
            print(f"create from creds {creds_doc['_id']}: {key}")
            # Claim the identity so a second creds document with the same one folds into
            # this record on the next pass rather than creating a rival of it.
            auth_by_identity[key] = {"_id": None, "creds": creds}
            if apply:
                now = datetime.datetime.now(datetime.UTC)
                await db[AUTH_COLLECTION].insert_one(
                    {
                        "user_id": creds_doc.get("user_id"),
                        "space_id": creds_doc.get("space_id"),
                        "app_name": creds_doc.get("app_name"),
                        "name": creds_doc.get("name"),
                        "creds": creds,
                        # Not minted yet. Empty, not absent: the readers check truthiness.
                        "auth": {},
                        "lookup": {},
                        "default": creds_doc.get("default", True),
                        "created_at": creds_doc.get("created_at") or now,
                        "updated_at": now,
                    }
                )
            continue

        if auth_doc.get("creds"):
            already += 1
            continue

        folded += 1
        print(f"{auth_doc['_id']}: creds from {creds_doc['_id']}  {key}")
        # Claim it in memory too, so a second creds document with the same identity is
        # reported as already carried rather than quietly overwriting the first.
        auth_doc["creds"] = creds
        if apply:
            await db[AUTH_COLLECTION].update_one(
                {"_id": auth_doc["_id"]}, {"$set": {"creds": creds}}
            )

    verb, verb2 = ("Folded", "created") if apply else ("Would fold", "would create")
    print(
        f"\nScanned {scanned} creds docs. {verb} {folded} onto existing records, "
        f"{verb2} {created} that had no auth document. {already} already carried creds."
    )
    if duplicate_auth:
        print(
            f"{len(duplicate_auth)} auth documents share an identity with another; "
            "the first of each was used. Worth a look before deleting the old collection."
        )
    if not apply:
        print("Dry run. Re-run with --apply to write.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    asyncio.run(main(parser.parse_args().apply))
