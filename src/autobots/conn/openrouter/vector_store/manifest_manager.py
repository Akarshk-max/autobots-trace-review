from typing import Any

from src.autobots.conn.openrouter.utils import to_plain


class VectorStoreManifestManager:
    @staticmethod
    def normalize(items: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for raw in items or []:
            item = to_plain(raw)
            if isinstance(item, dict):
                out.append(item)
        return out

    @staticmethod
    def find_entry(
        manifest: list[dict[str, Any]],
        *,
        source_url: str | None = None,
        prepared_url: str | None = None,
    ) -> dict[str, Any] | None:
        for item in manifest:
            if source_url and item.get("source_url") == source_url:
                return item
            if prepared_url and item.get("prepared_url") == prepared_url:
                return item
        return None

    @staticmethod
    def upsert_entry(
        manifest: list[dict[str, Any]],
        *,
        source_url: str,
        prepared_url: str | None = None,
        source: str | None = None,
        representation: str | None = None,
        status: str | None = None,
        turn: int,
        display_name: str | None = None,
        last_error: str | None = None,
        source_content_sha256: str | None = None,
        vector_store_file_id: str | None = None,
        openai_file_id: str | None = None,
        last_indexed_turn: int | None = None,
    ) -> list[dict[str, Any]]:
        existing = VectorStoreManifestManager.find_entry(
            manifest,
            source_url=source_url,
            prepared_url=prepared_url,
        )

        payload: dict[str, Any] = {
            "source_url": source_url,
            "prepared_url": prepared_url,
            "source": source,
            "representation": representation,
            "status": status,
            "display_name": display_name,
            "last_error": last_error,
            "source_content_sha256": source_content_sha256,
            "vector_store_file_id": vector_store_file_id,
            "openai_file_id": openai_file_id,
            "last_indexed_turn": last_indexed_turn,
            "last_seen_turn": turn,
        }

        if existing:
            existing.update({k: v for k, v in payload.items() if v is not None})
            existing["first_seen_turn"] = min(
                int(existing.get("first_seen_turn") or turn),
                turn,
            )
            return manifest

        manifest.append(
            {
                "first_seen_turn": turn,
                "last_seen_turn": turn,
                **{k: v for k, v in payload.items() if v is not None},
            }
        )
        return manifest

    @staticmethod
    def remove_entries(
        manifest: list[dict[str, Any]],
        vector_store_file_ids: list[str],
    ) -> list[dict[str, Any]]:
        target_ids = {str(x).strip() for x in vector_store_file_ids if str(x).strip()}
        if not target_ids:
            return manifest
        return [
            item
            for item in manifest
            if str(item.get("vector_store_file_id") or "").strip() not in target_ids
        ]

    @staticmethod
    def counts(manifest: Any) -> dict[str, int]:
        items = VectorStoreManifestManager.normalize(manifest)
        return {
            "indexed": sum(1 for item in items if item.get("status") == "indexed"),
            "prepared": sum(1 for item in items if item.get("status") == "prepared"),
            "pending": sum(1 for item in items if item.get("status") == "pending"),
            "failed": sum(1 for item in items if item.get("status") in {"failed", "unsupported"}),
        }
