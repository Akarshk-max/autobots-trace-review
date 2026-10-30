from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from src.autobots.ingestion.spreadsheet.utils import is_empty_cell


def build_merge_fill_map(
    ws,
    *,
    max_row_1based: int,
    max_col_1based: int,
    max_cells: int = 50_000,
    warnings: Optional[List[str]] = None,
    sheet_name: Optional[str] = None,
) -> Dict[Tuple[int, int], object]:
    """
    Build a bounded map {(row_1based, col_1based): anchor_value} for merged cells.

    We only use this for small header zones (top rows), so a simple mapping is fine.
    If merged ranges are huge, we cap the total mapped cells to avoid blowups.
    """
    out: Dict[Tuple[int, int], object] = {}
    total_cells = 0
    skipped_ranges = 0

    merged = getattr(ws, "merged_cells", None)
    ranges = getattr(merged, "ranges", None)
    if not ranges:
        return out

    for r in ranges:
        try:
            min_row = int(r.min_row)
            max_row = int(r.max_row)
            min_col = int(r.min_col)
            max_col = int(r.max_col)
        except Exception:
            continue

        if min_row > max_row or min_col > max_col:
            continue

        # Bound to the requested region
        if min_row > max_row_1based or min_col > max_col_1based:
            continue

        rr0 = min_row
        cc0 = min_col
        try:
            anchor = ws.cell(row=rr0, column=cc0).value
        except Exception:
            anchor = None

        if is_empty_cell(anchor):
            continue

        row_hi = min(max_row, max_row_1based)
        col_hi = min(max_col, max_col_1based)

        cells_in_range = (row_hi - rr0 + 1) * (col_hi - cc0 + 1)
        if total_cells + cells_in_range > max_cells:
            # Skip this oversized range to remain safe; continue evaluating other ranges.
            skipped_ranges += 1
            continue

        for rr in range(rr0, row_hi + 1):
            for cc in range(cc0, col_hi + 1):
                out[(rr, cc)] = anchor

        total_cells += cells_in_range

    if warnings is not None and skipped_ranges > 0:
        sn = f" '{sheet_name}'" if sheet_name else ""
        warnings.append(
            f"Some merged ranges in sheet{sn} were skipped due to merge-fill cap (skipped_ranges={skipped_ranges})."
        )
    return out


def apply_merge_fill_to_row(
    row_num_1based: int,
    values: list,
    merge_map: Dict[Tuple[int, int], object],
    *,
    max_col_1based: int,
) -> list:
    """
    Fill empty cells in 'values' using merge_map, bounded by max_col_1based.
    """
    if not merge_map:
        return values
    # Do not pad rows: padding causes unnecessary allocations and can be a footgun
    # if this helper is ever applied beyond the bounded header zone.
    lim = min(len(values), max_col_1based)
    for c1 in range(1, lim + 1):
        if c1 - 1 >= len(values):
            break
        if is_empty_cell(values[c1 - 1]):
            mv = merge_map.get((row_num_1based, c1))
            if mv is not None:
                values[c1 - 1] = mv
    return values
