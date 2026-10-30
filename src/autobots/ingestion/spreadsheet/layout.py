from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from src.autobots.ingestion.spreadsheet.utils import (
    excel_col_name,
    is_empty_cell,
    norm_text,
)


@dataclass(frozen=True)
class HeaderRegion:
    header_start_idx: int              # index in preview list
    header_row_count: int              # 1..max
    header_rows_1based: Tuple[int, int]  # (start,end) 1-based row numbers in sheet
    headers_flat: List[str]
    col_start_0: int
    col_end_excl_0: int


def _row_stats(row: Sequence[object], max_cols: int) -> Tuple[int, int, int, float, float, float]:
    """
    Returns:
      ne, str_count, num_count, density, str_ratio, num_ratio
    """
    r = list(row[:max_cols]) if row else []
    max_w = max(1, len(r))
    non_empty = [c for c in r if not is_empty_cell(c)]
    ne = len(non_empty)
    if ne == 0:
        return 0, 0, 0, 0.0, 0.0, 0.0

    str_count = 0
    num_count = 0
    for c in non_empty:
        if isinstance(c, (int, float)):
            num_count += 1
            continue
        if isinstance(c, str):
            s = c.strip()
            if not s:
                # skip
                continue
            try:
                float(s.replace(",", ""))
                num_count += 1
            except Exception:
                str_count += 1
            continue
        str_count += 1

    density = ne / max(1, max_w)
    str_ratio = str_count / max(1, ne)
    num_ratio = num_count / max(1, ne)
    return ne, str_count, num_count, density, str_ratio, num_ratio


def _header_score(row: Sequence[object], max_cols: int) -> float:
    ne, _, _, density, str_ratio, num_ratio = _row_stats(row, max_cols)
    if ne == 0:
        return -1.0
    base = (2.0 * density) + (2.0 * str_ratio) - (1.0 * num_ratio)

    # Penalty for "title rows" that become dense due to merged-cell fill:
    # if most non-empty cells are the same string, this is unlikely to be a header.
    # This keeps merge-fill benefits while preventing merged titles from winning.
    vals: List[str] = []
    r = list(row[:max_cols]) if row else []
    for c in r:
        if is_empty_cell(c):
            continue
        s = norm_text(c)
        if s:
            vals.append(s.lower())
    if len(vals) >= 6:
        # dominant repetition ratio
        from collections import Counter
        cnt = Counter(vals)
        dom = cnt.most_common(1)[0][1]
        dom_ratio = dom / max(1, len(vals))
        if dom_ratio >= 0.90:
            base -= 3.0 * dom_ratio
    return base


def _is_header_like(row: Sequence[object], max_cols: int) -> bool:
    ne, _, _, density, str_ratio, num_ratio = _row_stats(row, max_cols)
    if ne < 2:
        return False
    # Header rows tend to be string-heavy and not too sparse.
    return (density >= 0.15) and (str_ratio >= 0.50) and (num_ratio <= 0.60)

def _is_secondary_header_like(row: Sequence[object], max_cols: int) -> bool:
    """
    Stricter predicate used only for expanding header region downward.
    This reduces the chance we accidentally include the first data row as a header row.
    """
    ne, _, _, density, str_ratio, num_ratio = _row_stats(row, max_cols)
    if ne < 2:
        return False
    if density < 0.20:
        return False
    if str_ratio < 0.60:
        return False
    if num_ratio > 0.30:
        return False
    # Heuristic: header strings tend to be shorter than data strings.
    r = list(row[:max_cols]) if row else []
    lens = []
    for c in r:
        if isinstance(c, str) and c.strip():
            lens.append(len(c.strip()))
    if lens and (sum(lens) / len(lens)) > 40:
        return False
    return True

def flatten_header_rows(header_rows: List[Sequence[object]], max_cols: int) -> List[str]:
    """
    Flatten 1..N header rows into a single header string per column:
      ["Revenue", "Q1"] -> "Revenue / Q1"
    """
    out: List[str] = []
    for j in range(max_cols):
        parts: List[str] = []
        for r in header_rows:
            v = r[j] if j < len(r) else None
            if is_empty_cell(v):
                continue
            s = norm_text(v)
            if s:
                parts.append(s)
        if not parts:
            out.append(excel_col_name(j))
        else:
            out.append(" / ".join(parts))
    return out


def infer_table_span_from_header_rows(header_rows: List[Sequence[object]], max_cols: int) -> Tuple[int, int]:
    """
    Returns (col_start_0, col_end_excl_0) based on any non-empty header cell.
    """
    left = None
    right = 0
    for r in header_rows:
        n = min(len(r), max_cols)
        for j in range(n):
            if not is_empty_cell(r[j]):
                if left is None or j < left:
                    left = j
                if j + 1 > right:
                    right = j + 1
    if left is None:
        return 0, max(1, max_cols)
    if right <= left:
        right = left + 1
    return left, right


def detect_header_region(
    preview_rows: List[Sequence[object]],
    preview_row_nums_1based: List[int],
    max_cols_hard: int,
    max_header_rows: int = 3,
) -> Optional[HeaderRegion]:
    """
    Detect a header *region* (1..max_header_rows) and return flattened headers + table span.
    Returns None if no reasonable header candidate.
    """
    if not preview_rows:
        return None

    max_w = max((min(len(r), max_cols_hard) for r in preview_rows), default=0)
    if max_w <= 0:
        return None

    # Find best base header row
    best_idx = -1
    best_score = -1.0
    for i, row in enumerate(preview_rows):
        score = _header_score(row, max_w)
        if score > best_score:
            best_score = score
            best_idx = i

    if best_idx < 0:
        return None

    # Require at least a minimal signal
    ne, _, _, _, _, _ = _row_stats(preview_rows[best_idx], max_w)
    if ne < 2:
        return None

    # Expand upward by 1 if it's a group header (stringy, sparse, multi-col)
    start_idx = best_idx
    if best_idx > 0:
        up = preview_rows[best_idx - 1]
        ne_up, _, _, density_up, str_ratio_up, num_ratio_up = _row_stats(up, max_w)
        if ne_up >= 2 and str_ratio_up >= 0.70 and num_ratio_up <= 0.30 and density_up <= 0.40:
            start_idx = best_idx - 1

    # Expand downward for additional header rows
    header_row_count = 1
    while header_row_count < max_header_rows:
        nxt = start_idx + header_row_count
        if nxt >= len(preview_rows):
            break
        if not _is_secondary_header_like(preview_rows[nxt], max_w):
            break
        header_row_count += 1

    header_rows = [preview_rows[i] for i in range(start_idx, start_idx + header_row_count)]
    headers_flat = flatten_header_rows(header_rows, max_cols=max_w)
    col_start_0, col_end_excl_0 = infer_table_span_from_header_rows(header_rows, max_cols=max_w)

    # Convert to actual sheet row numbers
    start_row_1based = preview_row_nums_1based[start_idx]
    end_row_1based = preview_row_nums_1based[start_idx + header_row_count - 1]

    return HeaderRegion(
        header_start_idx=start_idx,
        header_row_count=header_row_count,
        header_rows_1based=(start_row_1based, end_row_1based),
        headers_flat=headers_flat,
        col_start_0=col_start_0,
        col_end_excl_0=col_end_excl_0,
    )


def is_blank_row(
    row: Sequence[object],
    *,
    col_start_0: int,
    col_end_excl_0: int,
    empty_ratio_threshold: float = 0.95,
    key_cols: int = 2,
) -> bool:
    if col_end_excl_0 <= col_start_0:
        return True
    # If key columns have content, treat as non-blank even if the row is sparse.
    # This prevents false table breaks in wide sparse tables where the first column carries the row label.
    for k in range(max(0, key_cols)):
        j = col_start_0 + k
        if j >= col_end_excl_0:
            break
        v = row[j] if j < len(row) else None
        if not is_empty_cell(v):
            return False
    total = col_end_excl_0 - col_start_0
    empty = 0
    for j in range(col_start_0, col_end_excl_0):
        v = row[j] if j < len(row) else None
        if is_empty_cell(v):
            empty += 1
    return (empty / max(1, total)) >= empty_ratio_threshold


def row_matches_header(
    row: Sequence[object],
    headers_flat: Sequence[str],
    *,
    col_start_0: int,
    col_end_excl_0: int,
    min_matches: int = 2,
    match_ratio: float = 0.80,
) -> bool:
    """
    Detect repeated header rows inside the data stream.
    Heuristic: compare normalized string values with header leaf text.
    """
    if col_end_excl_0 <= col_start_0:
        return False
    matches = 0
    present = 0
    for j in range(col_start_0, min(col_end_excl_0, len(headers_flat))):
        v = row[j] if j < len(row) else None
        if is_empty_cell(v):
            continue
        present += 1
        cell = norm_text(v).lower()
        if not cell:
            continue
        hdr = (headers_flat[j] if j < len(headers_flat) else excel_col_name(j)).lower()
        # use leaf part (after last "/")
        leaf = hdr.split("/")[-1].strip()
        if leaf and cell == leaf:
            matches += 1
    if present == 0:
        return False
    return matches >= min_matches and (matches / present) >= match_ratio
