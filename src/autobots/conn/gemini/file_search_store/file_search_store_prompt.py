from typing import Any
from urllib.parse import unquote, urlparse

from src.autobots.conn.gemini.chat_model_gemini import GeminiChatConfig
from src.autobots.conn.gemini.file_search_store.file_search_store_state import (
    normalize_manifest_items,
)

_SPREADSHEET_EXTENSIONS = {".csv", ".tsv", ".xls", ".xlsx"}

def _display_name_from_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return "(empty-url)"
    try:
        parsed = urlparse(value)
        path = unquote(parsed.path or "")
        name = path.split("/")[-1] if path else ""
        return name or value
    except Exception:
        return value


def _is_spreadsheet_like(url: str) -> bool:
    name = _display_name_from_url(url).lower()
    for ext in _SPREADSHEET_EXTENSIONS:
        if name.endswith(ext):
            return True
    return False


def build_file_search_store_context_block(*, config: GeminiChatConfig) -> dict[str, Any] | None:
    if not getattr(config, "file_search_store_enabled", True):
        return None

    store_ref = getattr(config, "file_search_store_ref", None)
    store_name = None
    if isinstance(store_ref, dict):
        store_name = store_ref.get("store_name") or store_ref.get("storeName")
    manifest = normalize_manifest_items(getattr(config, "file_search_store_manifest", None))

    def _status(item: dict[str, Any]) -> str:
        raw = str(item.get("status") or "").strip().lower()
        return raw or "unknown"

    indexed_count = sum(1 for item in manifest if _status(item) == "indexed")
    ingested_count = sum(1 for item in manifest if _status(item) == "ingested")
    pending_count = sum(1 for item in manifest if _status(item) == "pending")
    too_large_count = sum(1 for item in manifest if _status(item) == "too_large")
    skipped_tabular_count = sum(
        1 for item in manifest if _status(item) in {"skipped_tabular", "skip_tabular"}
    )
    failed_count = sum(
        1
        for item in manifest
        if _status(item)
        in {"failed", "unsupported", "error", "too_large"}
    )

    current_turn = int(getattr(config, "chat_turn", 0) or 0)
    ready_urls: list[str] = []
    seen: set[str] = set()
    for item in manifest:
        try:
            if int(item.get("last_seen_turn") or 0) != current_turn:
                continue
        except Exception:
            continue
        url = str(item.get("source_url") or "").strip()
        if not url or url in seen:
            continue
        if _status(item) not in {"ingested", "indexed"}:
            continue
        seen.add(url)
        ready_urls.append(url)

    lines = [
        "Persistent retrieval state:",
        f"- file_search_store_enabled: {bool(getattr(config, 'file_search_store_enabled', True))}",
        f"- file_search_store_name: {store_name}",
        f"- indexed_files_count: {indexed_count}",
        f"- ingested_files_count: {ingested_count}",
        f"- pending_files_count: {pending_count}",
        f"- failed_files_count: {failed_count}",
        f"- too_large_files_count: {too_large_count}",
        f"- skipped_tabular_files_count: {skipped_tabular_count}",
        "",
        "File-search-store-ready URLs for this turn:",
    ]

    if ready_urls:
        for url in ready_urls[:10]:
            lines.append(f"- {_display_name_from_url(url)}: {url}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Rules:",
            "- Current turn URLs are automatically ingested into the file-search store when possible. You can perform retrieval search on these ingested files.",
            "- Use gemini_file_search_store_tool.query_store to search across ingested/indexed files.",
            "- Use gemini_file_search_store_tool.ingest_files to add new URLs discovered mid-conversation.",
            "- Do NOT use gemini_file_search_store_tool for csv, tsv, xls, or xlsx files.",
            "  Instead use gemini_code_execution_tool to read/parse/analyze tabular data.",
        ]
    )
    current_turn_urls = [str(u or "").strip() for u in (getattr(config, "current_turn_urls", None) or [])]
    spreadsheet_urls = [u for u in current_turn_urls if u and _is_spreadsheet_like(u)]
    if spreadsheet_urls:
        lines.append("- Spreadsheet-like files detected in current turn; prefer code execution for those.")
    return {"text": "\n".join(lines)}