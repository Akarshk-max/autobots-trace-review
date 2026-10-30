#!/usr/bin/env python3
"""Seed Platform access for all Supabase Auth users into MongoDB.

This is a one-off maintenance script you can run locally.

What it does
------------
- Lists users from Supabase Auth (GoTrue) Admin API (requires service role key)
- Upserts MongoDB documents into the `Access` collection so every user has:
    subscription="Platform", status="valid", space_id=None

Safe to re-run: uses upserts.

How to run
----------
Option A (recommended in this repo):
    uv run python scripts/access_seed_autobots.py

Option B:
    python3 scripts/access_seed_autobots.py

Dependencies
------------
- requests
- pymongo

If you run outside the repo/venv:
    pip install requests pymongo
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
from pymongo import MongoClient, UpdateOne

from src.autobots.core.settings import SettingsProvider

# ============================
# CONFIG (fill these in)
# ============================
settings = SettingsProvider.sget()
SUPABASE_URL = settings.SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY = settings.SUPABASE_SERVICE_ROLE_KEY

MONGO_CONN = settings.MONGO_CONN
MONGO_DATABASE = settings.MONGO_DATABASE

ACCESS_COLLECTION = "Access"

# Supabase pagination
PER_PAGE = 1000

# If True, only prints counts and does not write to MongoDB
DRY_RUN = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_base_url(url: str) -> str:
    url = (url or "").strip()
    return url[:-1] if url.endswith("/") else url


def _fetch_supabase_users_page(*, page: int, per_page: int) -> list[dict[str, Any]]:
    """Fetch one page of Supabase Auth users via the Admin API."""

    supabase_url = _normalize_base_url(SUPABASE_URL)

    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Content-Type": "application/json",
    }

    resp = requests.get(
        f"{supabase_url}/auth/v1/admin/users",
        headers=headers,
        params={"page": page, "per_page": per_page},
        timeout=30,
    )
    resp.raise_for_status()

    data: Any = resp.json()
    # Some responses are {"users": [...]} and some are just [...]
    users = data.get("users") if isinstance(data, dict) else data
    return list(users or [])


def main() -> None:
    if not SUPABASE_URL or "YOUR_PROJECT_REF" in SUPABASE_URL:
        raise SystemExit("Please set SUPABASE_URL in scripts/access_seed_autobots.py")
    if not SUPABASE_SERVICE_ROLE_KEY or "YOUR_SUPABASE_SERVICE_ROLE_KEY" in SUPABASE_SERVICE_ROLE_KEY:
        raise SystemExit(
            "Please set SUPABASE_SERVICE_ROLE_KEY in scripts/access_seed_autobots.py"
        )
    if not MONGO_CONN:
        raise SystemExit("Please set MONGO_CONN in scripts/access_seed_autobots.py")
    if not MONGO_DATABASE:
        raise SystemExit("Please set MONGO_DATABASE in scripts/access_seed_autobots.py")

    mongo = MongoClient(MONGO_CONN)
    db = mongo[MONGO_DATABASE]
    access_col = db[ACCESS_COLLECTION]

    total_users_seen = 0
    total_upserts = 0
    total_matched = 0

    page = 1
    while True:
        users = _fetch_supabase_users_page(page=page, per_page=PER_PAGE)
        if not users:
            break

        ops: list[UpdateOne] = []
        now = _utcnow()

        for u in users:
            user_id = (u or {}).get("id")
            if not user_id:
                continue

            total_users_seen += 1

            ops.append(
                UpdateOne(
                    {
                        "user_id": str(user_id),
                        "space_id": None,
                        "subscription": "Platform",
                    },
                    {
                        "$setOnInsert": {
                            "user_id": str(user_id),
                            "space_id": None,
                            "subscription": "Platform",
                            "created_at": now,
                        },
                        "$set": {
                            "status": "valid",
                            "updated_at": now,
                        },
                    },
                    upsert=True,
                )
            )

        if ops:
            if DRY_RUN:
                print(f"[DRY_RUN] Would bulk_write {len(ops)} upserts (page={page})")
            else:
                result = access_col.bulk_write(ops, ordered=False)
                total_upserts += int(result.upserted_count or 0)
                total_matched += int(result.matched_count or 0)

        page += 1

    print("Done.")
    print(f"Supabase users seen: {total_users_seen}")
    if DRY_RUN:
        print("DRY_RUN=True, no writes performed.")
    else:
        print(f"Mongo upserts created: {total_upserts}")
        print(f"Mongo matched existing: {total_matched}")


if __name__ == "__main__":
    main()
