import argparse
import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass(frozen=True)
class Target:
    collection: str
    field: str


TARGETS: tuple[Target, ...] = (
    Target(collection="AppCreds", field="creds"),
    Target(collection="AppAuth", field="auth"),
)


def _ensure_repo_root_on_path() -> None:
    import sys

    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Encrypt plaintext secrets in MongoDB AppCreds/AppAuth collections.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually write updates to MongoDB. Without this flag, runs in dry-run mode.",
    )
    p.add_argument(
        "--only",
        choices=["all", "app_creds", "app_auth"],
        default="all",
        help="Which collection(s) to migrate.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional cap on number of documents updated per target. 0 = no limit.",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="Mongo cursor batch size.",
    )
    return p.parse_args()


def _select_targets(only: str) -> Iterable[Target]:
    if only == "app_creds":
        return (TARGETS[0],)
    if only == "app_auth":
        return (TARGETS[1],)
    return TARGETS


async def _migrate_target(*, db: Any, target: Target, apply: bool, limit: int, batch_size: int) -> None:
    from src.autobots.integrations.crypto.encoder_decoder import is_encrypted
    from src.autobots.integrations.crypto.helpers import encrypt_if_needed

    coll = db[target.collection]
    query = {target.field: {"$type": "object"}, f"{target.field}._enc": {"$ne": True}}
    projection = {target.field: 1}

    scanned = 0
    updated = 0
    skipped_non_dict = 0

    cursor = coll.find(query, projection=projection).batch_size(batch_size)

    async for doc in cursor:
        scanned += 1
        raw = doc.get(target.field)

        if not isinstance(raw, dict):
            skipped_non_dict += 1
            continue

        if is_encrypted(raw):
            continue

        encrypted = encrypt_if_needed(raw)
        if not apply:
            updated += 1
        else:
            rs = await coll.update_one({"_id": doc["_id"]}, {"$set": {target.field: encrypted}})
            if rs.modified_count == 1:
                updated += 1

        if updated and updated % 500 == 0:
            logger.info(
                f"{target.collection}.{target.field}: scanned={scanned} updated={updated}"
            )

        if limit and updated >= limit:
            break

    logger.info(
        f"{target.collection}.{target.field}: done scanned={scanned} updated={updated} "
        f"skipped_non_dict={skipped_non_dict} apply={apply}"
    )


async def main() -> None:
    _ensure_repo_root_on_path()
    args = _parse_args()

    from src.autobots import SettingsProvider
    from src.autobots.core.database.mongo_base import close_mongo_client, get_mongo_db
    from src.autobots.integrations.crypto.encoder_decoder import init_crypto_keys

    settings = SettingsProvider.sget()
    if not settings.ENCRYPTION_PUBLIC_KEY_B64 or not settings.ENCRYPTION_PRIVATE_KEY_B64:
        raise SystemExit(
            "Missing ENCRYPTION_PUBLIC_KEY_B64 / ENCRYPTION_PRIVATE_KEY_B64 in environment."
        )

    init_crypto_keys(settings.ENCRYPTION_PUBLIC_KEY_B64, settings.ENCRYPTION_PRIVATE_KEY_B64)

    db = next(get_mongo_db())
    try:
        logger.info(
            f"Starting secrets encryption migration: apply={args.apply} only={args.only} "
            f"limit={args.limit} batch_size={args.batch_size}"
        )
        for target in _select_targets(args.only):
            await _migrate_target(
                db=db,
                target=target,
                apply=args.apply,
                limit=args.limit,
                batch_size=args.batch_size,
            )
        logger.info("Migration finished")
    finally:
        close_mongo_client()


if __name__ == "__main__":
    asyncio.run(main())
