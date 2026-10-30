#!/usr/bin/env python3
"""Delete Platform access rows from MongoDB.

This is a one-off maintenance script you can run locally to remove all Access
rows that represent the default seeded Platform access.

What it does
------------
- Deletes documents in the Access collection where:
    subscription == "Platform"

Safe guards
----------
- DRY_RUN=True by default (no deletes)
- When DRY_RUN is False, requires you to set CONFIRM_DELETE="YES".

How to run
----------
Option A (recommended in this repo):
    uv run python scripts/access_delete_autobots.py

Option B:
    python3 scripts/access_delete_autobots.py

Dependencies
------------
- pymongo

If you run outside the repo/venv:
    pip install pymongo
"""

from __future__ import annotations

from pymongo import MongoClient

from src.autobots.core.settings import SettingsProvider

# ============================
# CONFIG (fill these in)
# ============================
settings = SettingsProvider.sget()
MONGO_CONN = settings.MONGO_CONN
MONGO_DATABASE = settings.MONGO_DATABASE

ACCESS_COLLECTION = "Access"

# If True, does not delete anything. Prints the would-delete count.
DRY_RUN = True

# When DRY_RUN is False, you must set this to "YES" or the script exits.
CONFIRM_DELETE = "No"


def main() -> None:
    if not MONGO_CONN:
        raise SystemExit("Please set MONGO_CONN in scripts/access_delete_autobots.py")
    if not MONGO_DATABASE:
        raise SystemExit("Please set MONGO_DATABASE in scripts/access_delete_autobots.py")

    if not DRY_RUN and CONFIRM_DELETE.strip().upper() != "YES":
        raise SystemExit(
            "Refusing to delete because DRY_RUN=False but CONFIRM_DELETE != 'YES'. "
            "Set CONFIRM_DELETE='YES' to proceed."
        )

    mongo = MongoClient(MONGO_CONN)
    db = mongo[MONGO_DATABASE]
    access_col = db[ACCESS_COLLECTION]
    filter_q = {"subscription": "Platform"}

    to_delete = int(access_col.count_documents(filter_q))
    print(f"Access rows with subscription='Platform': {to_delete}")

    if DRY_RUN:
        print("DRY_RUN=True, no deletes performed.")
        return

    result = access_col.delete_many(filter_q)
    print(f"Deleted: {int(result.deleted_count or 0)}")


if __name__ == "__main__":
    main()
