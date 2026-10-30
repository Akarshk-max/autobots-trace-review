import json
from typing import Any

from src.autobots.conn.gemini.chat_model_gemini import GeminiChatConfig
from src.autobots.conn.gemini.file_search_store.file_search_store_models import (
    GeminiFileSearchStoreRef,
    GeminiFileSearchStoreRes,
)


def _norm(value: Any) -> str | None:
    value = str(value or "").strip()
    return value or None


def normalize_manifest_items(items: list[Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw_item in items or []:
        if hasattr(raw_item, "model_dump"):
            out.append(raw_item.model_dump(exclude_none=True))
        elif isinstance(raw_item, dict):
            out.append(dict(raw_item))
    return out


def upsert_file_search_store_manifest_entry(
    manifest: list[dict[str, Any]],
    *,
    source_url: str | None = None,
    document_name: str | None = None,
    gemini_file_name: str | None = None,
    updates: dict[str, Any],
) -> None:
    """Claude-like upsert semantics.

    Claude matches any existing manifest item by *any* stable identifier for the file:
    - source_url
    - prepared_url
    - vector_store_file_id
    - openai_file_id

    For Gemini file search store we treat the equivalents as:
    - source_url
    - gemini_file_name (original file handle)
    - document_name (store-ingested document id)

    If an entry matches, we update it in place; otherwise we append a new entry.
    """

    source_url = _norm(source_url)
    document_name = _norm(document_name)
    gemini_file_name = _norm(gemini_file_name)

    def _matches(item: dict[str, Any]) -> bool:
        if source_url and _norm(item.get("source_url")) == source_url:
            return True

        existing_document_name = _norm(item.get("document_name"))
        if document_name and existing_document_name == document_name:
            return True

        existing_gemini_file_name = _norm(item.get("gemini_file_name"))
        if gemini_file_name and existing_gemini_file_name == gemini_file_name:
            return True

        return False

    identifier_keys = {"source_url", "document_name", "gemini_file_name"}
    normalized_updates = {
        k: v
        for k, v in (updates or {}).items()
        if v is not None and k not in identifier_keys
    }

    for item in manifest:
        if not isinstance(item, dict):
            continue
        if _matches(item):
            item.update(normalized_updates)

            # Fill in canonical identifiers if missing.
            if source_url and not item.get("source_url"):
                item["source_url"] = source_url
            if gemini_file_name:
                item["gemini_file_name"] = gemini_file_name

            # For the store-ingested identifier, prefer the most recent value.
            if document_name:
                item["document_name"] = document_name
            return

    base: dict[str, Any] = {
        "source_url": source_url,
        "document_name": document_name,
        "gemini_file_name": gemini_file_name,
    }
    base.update(normalized_updates)
    manifest.append({k: v for k, v in base.items() if v is not None})


def merge_file_search_store_manifest(
    *,
    existing: list[Any] | None,
    incoming: list[Any] | None,
) -> list[dict[str, Any]]:
    existing_manifest = normalize_manifest_items(existing)
    incoming_manifest = normalize_manifest_items(incoming)
    if not incoming_manifest:
        return existing_manifest

    # Preserve ordering: keep existing order, and append new items in incoming order.
    out = [dict(item) for item in existing_manifest]
    for item in incoming_manifest:
        upsert_file_search_store_manifest_entry(
            out,
            source_url=item.get("source_url"),
            gemini_file_name=item.get("gemini_file_name"),
            document_name=item.get("document_name"),
            updates=item,
        )
    return out


def remove_file_search_store_manifest_entries(
    *,
    manifest: list[Any] | None,
    document_names: list[str] | None,
) -> list[dict[str, Any]]:
    target_ids = {_norm(x) for x in (document_names or []) if _norm(x)}
    if not target_ids:
        return normalize_manifest_items(manifest)

    out: list[dict[str, Any]] = []
    for item in normalize_manifest_items(manifest):
        if _norm(item.get("document_name")) in target_ids:
            continue
        out.append(item)
    return out


def apply_file_search_store_tool_state_update(
    *,
    config: GeminiChatConfig,
    tool_text: str,
    current_turn: int,
) -> None:
    """Update GeminiChatConfig state after gemini_file_search_store_tool calls (Claude-style)."""

    try:
        payload = json.loads(tool_text or "{}")
    except Exception:
        return

    if not isinstance(payload, dict):
        return

    try:
        res = GeminiFileSearchStoreRes.model_validate(payload)
    except Exception:
        return

    operation = res.operation

    if operation == "create_store" and isinstance(res.store, dict):
        store_name = _norm(res.store.get("name"))
        if store_name:
            ref = GeminiFileSearchStoreRef(
                scope="chat_scoped" if getattr(config, "chat_id", None) else "named_general_store",
                store_name=store_name,
                display_name=res.store.get("display_name") or res.store.get("displayName"),
                chat_id=getattr(config, "chat_id", None),
                metadata={
                    "embedding_model": res.store.get("embedding_model") or "models/gemini-embedding-2"
                },
            ).model_dump(exclude_none=True)
            config.file_search_store_ref = ref

    if operation == "delete_store":
        config.file_search_store_ref = None
        config.file_search_store_manifest = []
        return

    if operation in {"ingest_files", "reindex_documents", "delete_documents"}:
        incoming_docs: list[dict[str, Any]] = []
        for raw_doc in res.documents or []:
            doc = raw_doc.model_dump(exclude_none=True) if hasattr(raw_doc, "model_dump") else dict(raw_doc)
            doc["last_seen_turn"] = current_turn
            incoming_docs.append(doc)

        if operation == "delete_documents":
            config.file_search_store_manifest = remove_file_search_store_manifest_entries(
                manifest=getattr(config, "file_search_store_manifest", None),
                document_names=[d.get("document_name") for d in incoming_docs if d.get("document_name")],
            )
        else:
            config.file_search_store_manifest = merge_file_search_store_manifest(
                existing=getattr(config, "file_search_store_manifest", None),
                incoming=incoming_docs,
            )

        config.file_search_store_last_indexed_turn = current_turn
