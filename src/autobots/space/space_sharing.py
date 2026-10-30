"""Utilities for unified shared_with: supports both emails (str) and SpaceId entries.

Storage uses a mixed list:
    shared_with = ["alice@example.com", {"space_id": "sp_abc123"}]
"""

from src.autobots.data_model.space_id import SpaceId


def get_space_ids_from_shared_with(shared_with: list[SpaceId | str] | None) -> list[str]:
    """Extract all space IDs from a shared_with list."""
    if not shared_with:
        return []
    return [
        entry.space_id
        for entry in shared_with
        if isinstance(entry, SpaceId) and entry.space_id
    ]


def get_emails_from_shared_with(shared_with: list[SpaceId | str] | None) -> list[str]:
    """Extract all email entries from a shared_with list."""
    if not shared_with:
        return []
    return [entry for entry in shared_with if isinstance(entry, str)]
