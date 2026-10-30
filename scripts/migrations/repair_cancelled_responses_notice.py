#!/usr/bin/env python3
"""Repair OpenAI Responses configs poisoned by an invalid cancellation notice.

The first cancellation release appended the notice to a Responses `input` list as

    {"role": "assistant", "content": [{"type": "output_text", "text": ...}]}

which is not a legal Responses input item: EasyInputMessageParam.content only
accepts a string or *input*-type content blocks, and ResponseOutputMessageParam
additionally requires id/status/type. The run cancels fine, but every following
turn dies in LlmResponsesOpenaiConfig.model_validate at action_factory.run_action,
so the action result is permanently unusable.

This rewrites those items to the legal EasyInputMessageParam form, preserving the
notice. Dry-run by default.

Usage
-----
    python -m scripts.migrations.repair_cancelled_responses_notice
    python -m scripts.migrations.repair_cancelled_responses_notice --apply
"""

import argparse
import asyncio

from src.autobots.core.database.mongo_base import get_mongo_client
from src.autobots.core.settings import SettingsProvider

COLLECTION = "EventResults"


def _is_poisoned(item) -> bool:
    """Only the malformed notice, never a real assistant output.

    A genuine assistant turn is a ResponseOutputMessageParam and carries id,
    status and type; those are exactly the fields the bad notice omitted, so
    requiring their absence keeps real messages untouched.
    """
    if not isinstance(item, dict) or item.get("role") != "assistant":
        return False
    if any(field in item for field in ("id", "status", "type")):
        return False
    content = item.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return False
    block = content[0]
    return isinstance(block, dict) and block.get("type") == "output_text"


def _repaired(item) -> dict:
    text = " ".join(
        block.get("text", "")
        for block in item.get("content", [])
        if isinstance(block, dict) and block.get("type") == "output_text"
    ).strip()
    return {"role": "assistant", "content": text, "type": "message"}


async def main(apply: bool) -> None:
    SettingsProvider.set()
    db = get_mongo_client()[SettingsProvider.sget().MONGO_DATABASE]

    scanned = 0
    poisoned = 0
    repaired_items = 0

    cursor = db[COLLECTION].find({"result.config.input": {"$exists": True}})
    async for doc in cursor:
        scanned += 1
        items = doc.get("result", {}).get("config", {}).get("input")
        if not isinstance(items, list):
            continue

        bad_indexes = [i for i, item in enumerate(items) if _is_poisoned(item)]
        if not bad_indexes:
            continue

        poisoned += 1
        repaired_items += len(bad_indexes)
        print(f"{doc['_id']}: {len(bad_indexes)} poisoned item(s) at {bad_indexes}")

        if apply:
            for i in bad_indexes:
                items[i] = _repaired(items[i])
            await db[COLLECTION].update_one(
                {"_id": doc["_id"]}, {"$set": {"result.config.input": items}}
            )

    verb = "Repaired" if apply else "Would repair"
    print(f"\nScanned {scanned} docs. {verb} {repaired_items} item(s) across {poisoned} doc(s).")
    if not apply:
        print("Dry run. Re-run with --apply to write.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    asyncio.run(main(parser.parse_args().apply))
