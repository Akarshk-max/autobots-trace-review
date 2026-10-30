import hashlib
import os
from typing import Any, Dict

from loguru import logger


def get_process_rss_bytes() -> int | None:
    try:
        import psutil  # type: ignore

        p = psutil.Process(os.getpid())
        return int(p.memory_info().rss)
    except Exception as e:
        logger.bind(err=str(e)).debug("Unable to read process RSS")
        return None


def format_bytes(num_bytes: int | None) -> str | None:
    if num_bytes is None:
        return None
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    idx = 0
    while size >= 1024.0 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    return f"{size:.2f}{units[idx]}"


def query_log_fields(q: str, *, preview_len: int = 64) -> Dict[str, Any]:
    q = (q or "").strip()
    sha = hashlib.sha1(q.encode("utf-8")).hexdigest()[:12] if q else ""
    preview = q[:preview_len]
    return {
        "q_len": len(q),
        "q_sha1": sha,
        "q_preview": preview,
        "q_tokens": len(q.split()) if q else 0,
    }
