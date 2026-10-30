from __future__ import annotations

import re
from typing import List, Sequence, Tuple

_WS_RE = re.compile(r"\s+")

# A slightly stronger injection-token scrub than the earlier spreadsheet-only "<|" / "###".
# This mirrors the intent of _JSON_INJECTION_TOKENS in service.py but stays local to spreadsheet module.
INJECTION_TOKENS: Tuple[str, ...] = ("<|", "###", "SYSTEM:", "ASSISTANT:", "USER:", "DEVELOPER:")


def norm_text(s: object) -> str:
    """Normalize whitespace for stable chunk text rendering."""
    if s is None:
        return ""
    out = str(s).strip()
    out = _WS_RE.sub(" ", out)
    return out


def sanitize_text(s: str) -> str:
    """Remove prompt-injection-ish tokens (best-effort)."""
    out = s
    for tok in INJECTION_TOKENS:
        out = out.replace(tok, "")
    return out


def is_empty_cell(v: object) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    return False


def excel_col_name(idx0: int) -> str:
    """0-based index -> Excel letters."""
    n = idx0 + 1
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def col_range_str(col_start_0: int, col_end_excl_0: int) -> str:
    """0-based [start,end) -> 'A-D'."""
    if col_end_excl_0 <= col_start_0:
        return excel_col_name(col_start_0)
    a = excel_col_name(col_start_0)
    b = excel_col_name(col_end_excl_0 - 1)
    return f"{a}-{b}" if a != b else a


def rightmost_nonempty_index_1based(row: Sequence[object], max_cols: int) -> int:
    """Return 1-based index of last non-empty cell within max_cols, else 0."""
    n = min(len(row), max_cols)
    for j in range(n, 0, -1):
        if not is_empty_cell(row[j - 1]):
            return j
    return 0


def nonempty_ratio(row: Sequence[object], col_start_0: int, col_end_excl_0: int) -> float:
    """Compute non-empty ratio within [col_start, col_end)."""
    if col_end_excl_0 <= col_start_0:
        return 0.0
    total = max(1, col_end_excl_0 - col_start_0)
    ne = 0
    for j in range(col_start_0, col_end_excl_0):
        v = row[j] if j < len(row) else None
        if not is_empty_cell(v):
            ne += 1
    return ne / total


def window_headers_with_letters(headers: List[str], col_start_0: int, col_end_excl_0: int) -> str:
    """
    Format: D=Revenue | E=Q1 | F=Q2 ...
    """
    parts: List[str] = []
    for j in range(col_start_0, col_end_excl_0):
        col_letter = excel_col_name(j)
        name = headers[j] if j < len(headers) and headers[j] else col_letter
        parts.append(f"{col_letter}={name}")
    return " | ".join(parts)
